-- Repair hosted function drift from the legacy SELECT 1 migration.
-- Preserve existing function privileges and all diagnostic records.

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
   if r.status='running' and r.started_at > now()-interval '240 seconds' then
     pending:=true; msg:='Waiting for the Mac. It polls every 30 seconds; this request expires after 240 seconds.';
   elsif r.status='running' then
     msg:='Timed out: the Mac did not upload a matching result within 240 seconds. It may be asleep, offline, or its worker may have stopped. Press Check now to retry.';
   elsif r.finished_at is not null and r.finished_at>=r.started_at then
     checked:=r.finished_at;
     duration:=least(180000,greatest(0,coalesce((r.meta->>'latency_ms')::integer,0)));
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
         when 'timeout' then 'ClickHouse wake-up or table checks timed out. Cloud wake-up allows 120 seconds, then table checks have a 20-second budget.'
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

create or replace function public.amem_connection_probe_finish(p_request_id uuid,p_healthy boolean,p_latency_ms integer,p_failure_code text) returns boolean
language plpgsql security definer set search_path=pg_catalog,public,pg_temp as $$
declare changed integer;
begin
 if p_request_id is null or p_healthy is null or p_latency_ms is null or p_latency_ms not between 0 and 180000
  or p_failure_code is null or p_failure_code not in ('ready','ok','auth','http','connection','configuration','unexpected','unknown','timeout','permissions','schema','rate') then return false; end if;
 if p_healthy and p_failure_code<>'ready' then return false; end if;
 update public.rsi_checkpoints set status=case when p_healthy then 'done' else 'failed' end,
  finished_at=clock_timestamp(),meta=meta||jsonb_build_object('healthy',p_healthy,'latency_ms',p_latency_ms,'failure_code',p_failure_code)
  where job='connections:clickhouse-probe' and run_key='live' and status='running'
   and meta->>'request_id'=p_request_id::text and started_at>now()-interval '240 seconds';
 get diagnostics changed=row_count; return changed=1;
end $$;

notify pgrst, 'reload schema';
