-- One bounded, nonce-matched ClickHouse request. No user SQL or model prompts.
create function public.amem_connection_diagnostic_start() returns jsonb
language plpgsql security definer set search_path=pg_catalog,public,pg_temp as $$
declare r public.rsi_checkpoints%rowtype; nonce uuid; requested timestamptz;
begin
  perform pg_advisory_xact_lock(723914210);
  select * into r from public.rsi_checkpoints where job='connections:clickhouse-probe' and run_key='live' for update;
  if found and r.meta->>'format'='night-desk-integrations/2' and (r.status='running' or (r.meta->>'claimed'='true' and not(r.meta ? 'langfuse'))) and r.started_at > now()-interval '90 seconds' then
    return jsonb_build_object('request_id',r.meta->>'request_id','requested_at',r.started_at);
  end if;
  nonce:=gen_random_uuid(); requested:=clock_timestamp();
  insert into public.rsi_checkpoints(job,run_key,status,started_at,finished_at,meta)
    values('connections:clickhouse-probe','live','running',requested,null,jsonb_build_object('request_id',nonce,'format','night-desk-integrations/2'))
    on conflict(job,run_key) do update set status='running',started_at=excluded.started_at,finished_at=null,meta=excluded.meta;
  return jsonb_build_object('request_id',nonce,'requested_at',requested);
end $$;

revoke all on function public.amem_connection_diagnostic_start() from public;
do $$ declare r text; begin
 foreach r in array array['anon','authenticated','amem_app','rsi_reader','rsi_writer','service_role'] loop
  if exists(select 1 from pg_roles where rolname=r) then execute format('revoke all on function public.amem_connection_diagnostic_start() from %I',r); end if;
 end loop;
 if exists(select 1 from pg_roles where rolname='service_role') then grant execute on function public.amem_connection_diagnostic_start() to service_role; end if;
end $$;

-- Read-only status never queues another ClickHouse request. Failed outcomes remain visible.
create or replace function public.amem_connection_probe_result(p_request_id uuid) returns jsonb
language plpgsql stable security definer set search_path=pg_catalog,public,pg_temp as $$
declare r public.rsi_checkpoints%rowtype; pending boolean:=false; state text:='unknown';
 msg text:='No matching ClickHouse result is available. Press Check now to try again.';
 checked timestamptz:=now(); duration integer:=0; requested timestamptz; code text;
begin
 select * into r from public.rsi_checkpoints where job='connections:clickhouse-probe' and run_key='live' and meta->>'request_id'=p_request_id::text;
 if found and coalesce(r.meta->>'format','')<>'night-desk-integrations/2' then
  return jsonb_build_object('request_id',p_request_id,'requested_at',r.started_at,'pending',false,'check',
    jsonb_build_object('id','clickhouse','status','unknown','checked_at',coalesce(r.finished_at,r.started_at),'latency_ms',0,
      'message','The previous test checked basic query access only. Press Check now to verify the nightly tables and read/write permissions.'));
 end if;
 if found then
   requested:=r.started_at; checked:=r.started_at;
   if r.status='running' and r.started_at > now()-interval '90 seconds' then
     pending:=true; msg:='Waiting for the Mac. It polls every 30 seconds; this request expires after 90 seconds.';
   elsif r.status='running' then
     msg:='Timed out: the Mac did not upload a matching result within 90 seconds. It may be asleep, offline, or its worker may have stopped. Press Check now to retry.';
   elsif r.finished_at is not null and r.finished_at>=r.started_at then
     checked:=r.finished_at;
     duration:=least(120000,greatest(0,coalesce((r.meta->>'latency_ms')::integer,0)));
     if r.status='done' and r.meta->'healthy'='true'::jsonb then
       if r.finished_at>now()-interval '90 seconds' then
         state:='healthy'; msg:='The Mac verified the nightly ClickHouse tables, required columns, SELECT access and zero-row INSERT permission.';
       else
         state:='stale'; msg:='The last nightly-table access test passed, but is over 90 seconds old. Press Check now for a fresh test.';
       end if;
     else
       state:='unhealthy'; code:=r.meta->>'failure_code';
       msg:=case code
         when 'schema' then 'A nightly ClickHouse table or required column is missing. Sync cannot use the current schema.'
         when 'permissions' then 'The ClickHouse account cannot SELECT or INSERT into a table required by the nightly adapter.'
         when 'rate' then 'ClickHouse rate-limited the diagnostic. Wait before trying again.'
         when 'auth' then 'ClickHouse rejected the credentials or permissions (HTTP 401/403). Check the Mac account access.'
         when 'timeout' then 'The ClickHouse connection or response timed out. The Mac uses a 20-second network timeout.'
         when 'connection' then 'The Mac could not connect securely to ClickHouse. Check its network, DNS and TLS connection.'
         when 'configuration' then 'The Mac ClickHouse endpoint or credentials are missing or invalid.'
         when 'unexpected' then 'ClickHouse returned an unexpected response to the fixed nightly-table readiness queries.'
         when 'http' then 'ClickHouse returned an HTTP error other than 401/403. Check service availability or rate limits.'
         else 'The Mac reported a ClickHouse query failure without a confirmed cause.' end;
     end if;
   else
     msg:='The Mac result has an invalid completion time. Press Check now to retry.';
   end if;
 end if;
 return jsonb_build_object('request_id',p_request_id,'requested_at',requested,'pending',pending,
   'check',jsonb_build_object('id','clickhouse','status',state,'checked_at',checked,'latency_ms',duration,'message',msg));
end $$;

create function public.amem_connection_probe_current() returns jsonb
language plpgsql stable security definer set search_path=pg_catalog,public,pg_temp as $$
declare id uuid;
begin
 select (meta->>'request_id')::uuid into id from public.rsi_checkpoints where job='connections:clickhouse-probe' and run_key='live';
 return public.amem_connection_probe_result(id);
end $$;
revoke all on function public.amem_connection_probe_current() from public;
do $$ declare role_name text; begin
 foreach role_name in array array['anon','authenticated','amem_app','rsi_reader','rsi_writer','service_role'] loop
  if exists(select 1 from pg_roles where rolname=role_name) then
   execute format('revoke all on function public.amem_connection_probe_current() from %I',role_name);
  end if;
 end loop;
 if exists(select 1 from pg_roles where rolname='service_role') then
  grant execute on function public.amem_connection_probe_current() to service_role;
 end if;
end $$;

-- Accept a fixed timeout classification from the trusted Mac, with no raw errors.
create or replace function public.amem_connection_probe_finish(p_request_id uuid,p_healthy boolean,p_latency_ms integer,p_failure_code text) returns boolean
language plpgsql security definer set search_path=pg_catalog,public,pg_temp as $$
declare changed integer;
begin
 if p_request_id is null or p_healthy is null or p_latency_ms is null or p_latency_ms not between 0 and 120000
  or p_failure_code is null or p_failure_code not in ('ready','ok','auth','http','connection','configuration','unexpected','unknown','timeout','permissions','schema','rate') then return false; end if;
 if p_healthy and p_failure_code<>'ready' then return false; end if;
 update public.rsi_checkpoints set status=case when p_healthy then 'done' else 'failed' end,
  finished_at=clock_timestamp(),meta=meta||jsonb_build_object('healthy',p_healthy,'latency_ms',p_latency_ms,'failure_code',p_failure_code)
  where job='connections:clickhouse-probe' and run_key='live' and status='running'
   and meta->>'request_id'=p_request_id::text and started_at>now()-interval '90 seconds';
 get diagnostics changed=row_count; return changed=1;
end $$;
notify pgrst,'reload schema';

-- The same manual diagnostic request verifies Langfuse's real trace/score pipeline on the Mac.
create function public.amem_connection_probe_claim() returns uuid
language sql security definer set search_path=pg_catalog,public,pg_temp as $$
 update public.rsi_checkpoints set meta=meta||'{"claimed":true}'::jsonb
 where job='connections:clickhouse-probe' and run_key='live' and status='running'
 and meta->>'format'='night-desk-integrations/2' and started_at>now()-interval '90 seconds' and coalesce(meta->>'claimed','false')='false'
 returning (meta->>'request_id')::uuid;
$$;
create function public.amem_connection_langfuse_finish(p_request_id uuid,p_code text,p_latency_ms integer) returns boolean
language plpgsql security definer set search_path=pg_catalog,public,pg_temp as $$
declare n integer;
begin
 if p_code is null or p_code not in ('ok','auth','timeout','connection','configuration','rejected','unexpected','accepted_pending','rate')
  or p_latency_ms is null or p_latency_ms not between 0 and 90000 then return false; end if;
 update public.rsi_checkpoints set meta=meta||jsonb_build_object('langfuse',jsonb_build_object('code',p_code,'latency_ms',p_latency_ms,'checked_at',clock_timestamp()))
 where job='connections:clickhouse-probe' and run_key='live' and meta->>'request_id'=p_request_id::text
 and not(meta ? 'langfuse') and started_at>now()-interval '90 seconds';
 get diagnostics n=row_count; return n=1;
end $$;
create function public.amem_connection_langfuse_result(p_request_id uuid) returns jsonb
language plpgsql stable security definer set search_path=pg_catalog,public,pg_temp as $$
declare r public.rsi_checkpoints%rowtype; result jsonb; code text; state text:='unknown'; pending boolean:=false;
 checked timestamptz:=now(); latency integer:=0; msg text:='Not tested. Press Check now to send a labelled diagnostic trace and score; no model call is made.';
begin
 select * into r from public.rsi_checkpoints where job='connections:clickhouse-probe' and run_key='live' and meta->>'request_id'=p_request_id::text and meta->>'format'='night-desk-integrations/2';
 if found then
  checked:=r.started_at; result:=r.meta->'langfuse';
  if result is null then
   if r.started_at>now()-interval '90 seconds' then pending:=true; msg:='Waiting for the Mac to send and read back a labelled diagnostic trace and score.';
   else msg:='Timed out: the Mac did not confirm the Langfuse diagnostic within 90 seconds. Check that it is awake and online.'; end if;
  else
   code:=result->>'code'; checked:=(result->>'checked_at')::timestamptz; latency:=(result->>'latency_ms')::integer;
   state:=case when code='ok' and checked>now()-interval '90 seconds' then 'healthy' when code='ok' then 'stale' when code='accepted_pending' then 'unknown' else 'unhealthy' end;
   msg:=case code
    when 'ok' then 'Langfuse accepted the nightly-format diagnostic trace and score, and both were read back by their fresh IDs. No model was called.'
    when 'rate' then 'Langfuse rate-limited ingestion or read-back. Wait before trying again.'
    when 'auth' then 'Langfuse rejected its credentials or project permissions (HTTP 401/403).'
    when 'rejected' then 'Langfuse rejected one or more diagnostic spans or scores. Authentication alone passed; the nightly ingestion format did not.'
    when 'timeout' then 'The Langfuse ingestion or read-back request timed out.'
    when 'connection' then 'The Mac could not connect to the configured Langfuse service.'
    when 'configuration' then 'The Mac Langfuse endpoint or project credentials are missing or invalid.'
    when 'accepted_pending' then 'Langfuse accepted the upload, but the fresh trace or score was not visible within the read-back window. Delivery is not verified.'
    else 'Langfuse returned an unexpected ingestion or read-back response. The nightly data path is not verified.' end;
  end if;
 end if;
 return jsonb_build_object('pending',pending,'check',jsonb_build_object('id','langfuse','status',state,'checked_at',checked,'latency_ms',latency,'message',msg));
end $$;
do $$ declare fn text; role_name text; begin
 foreach fn in array array['public.amem_connection_probe_claim()','public.amem_connection_langfuse_finish(uuid,text,integer)','public.amem_connection_langfuse_result(uuid)'] loop
  execute 'revoke all on function '||fn||' from public';
  foreach role_name in array array['anon','authenticated','amem_app','rsi_reader','rsi_writer','service_role'] loop
   if exists(select 1 from pg_roles where rolname=role_name) then execute format('revoke all on function %s from %I',fn,role_name); end if;
  end loop;
 end loop;
 if exists(select 1 from pg_roles where rolname='amem_app') then
  grant execute on function public.amem_connection_probe_claim() to amem_app;
  grant execute on function public.amem_connection_langfuse_finish(uuid,text,integer) to amem_app;
 end if;
 if exists(select 1 from pg_roles where rolname='service_role') then grant execute on function public.amem_connection_langfuse_result(uuid) to service_role; end if;
end $$;
notify pgrst,'reload schema';
