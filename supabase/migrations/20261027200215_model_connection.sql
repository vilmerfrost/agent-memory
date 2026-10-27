-- Fixed manual subscription diagnostics. No caller-provided prompts, models or credentials.
create table amem_private.model_connection_receipts(request_id uuid primary key, provider text not null);
revoke all on amem_private.model_connection_receipts from public;
create table amem_private.model_connection_tests (
 provider text primary key check(provider in ('codex','claude')),
 request_id uuid not null unique,
 requested_at timestamptz not null default clock_timestamp(),
 claimed_at timestamptz,
 claim_id uuid,
 finished_at timestamptz,
 result jsonb
);
revoke all on amem_private.model_connection_tests from public;

create function public.amem_model_test_start(p_provider text,p_request_id uuid) returns jsonb
language plpgsql security definer set search_path=pg_catalog,pg_temp as $$
declare r amem_private.model_connection_tests%rowtype;
begin
 if p_provider is null or p_provider not in ('codex','claude') or p_request_id is null then raise exception 'invalid request'; end if;
 perform pg_advisory_xact_lock(723914211);
 select * into r from amem_private.model_connection_tests where provider=p_provider for update;
 if exists(select 1 from amem_private.model_connection_receipts where request_id=p_request_id) or (r.provider is not null and r.requested_at>clock_timestamp()-interval '5 minutes') then
   return jsonb_build_object('request_id',r.request_id,'accepted',coalesce(r.request_id=p_request_id,false),'retry_at',r.requested_at+interval '5 minutes');
 end if;
 insert into amem_private.model_connection_receipts values(p_request_id,p_provider);
 insert into amem_private.model_connection_tests(provider,request_id) values(p_provider,p_request_id)
 on conflict(provider) do update set request_id=excluded.request_id,requested_at=clock_timestamp(),claimed_at=null,claim_id=null,finished_at=null,result=null;
 return jsonb_build_object('request_id',p_request_id,'accepted',true);
end $$;

create function public.amem_model_test_claim() returns jsonb
language plpgsql security definer set search_path=pg_catalog,pg_temp as $$
declare r amem_private.model_connection_tests%rowtype;
begin
 select * into r from amem_private.model_connection_tests where claimed_at is null and requested_at>clock_timestamp()-interval '3 minutes'
 order by requested_at for update skip locked limit 1;
 if not found then return null; end if;
 update amem_private.model_connection_tests set claimed_at=clock_timestamp(),claim_id=gen_random_uuid() where provider=r.provider returning * into r;
 return jsonb_build_object('provider',r.provider,'request_id',r.request_id,'claim_id',r.claim_id);
end $$;

create function public.amem_model_test_finish(p_request_id uuid,p_claim_id uuid,p_result jsonb) returns boolean
language plpgsql security definer set search_path=pg_catalog,pg_temp as $$
declare n integer; r amem_private.model_connection_tests%rowtype;
begin
 if p_result is null or jsonb_typeof(p_result)<>'object' or
    not(p_result ?& array['code','model','latency_ms','tokens_in','tokens_out']) or
    p_result - array['code','model','latency_ms','tokens_in','tokens_out'] <> '{}'::jsonb or
    p_result->>'code' not in ('ok','configuration','auth','busy','limit','timeout','model','reply','tools','usage','client','unknown') or
    jsonb_typeof(p_result->'model')<>'string' or
    jsonb_typeof(p_result->'code')<>'string' or
    jsonb_typeof(p_result->'latency_ms')<>'number' or
    jsonb_typeof(p_result->'tokens_in')<>'number' or jsonb_typeof(p_result->'tokens_out')<>'number' or
    (p_result->>'latency_ms') !~ '^[0-9]{1,6}$' or (p_result->>'latency_ms')::integer>90000 or
    (p_result->>'tokens_in') !~ '^[0-9]{1,9}$' or (p_result->>'tokens_out') !~ '^[0-9]{1,9}$' then return false; end if;
 select * into r from amem_private.model_connection_tests where request_id=p_request_id and claim_id=p_claim_id for update;
 if not found then return false; end if;
 if r.result is not null then return r.result=p_result; end if;
 if r.claimed_at is null or r.requested_at<clock_timestamp()-interval '3 minutes' then return false; end if;
 if p_result->>'model'<>(case r.provider when 'codex' then 'gpt-6.1-sol' else 'claude-opus-5-5' end) then return false; end if;
 if p_result->>'code'='ok' and ((p_result->>'tokens_in')::bigint+(p_result->>'tokens_out')::bigint=0) then return false; end if;
 update amem_private.model_connection_tests set finished_at=clock_timestamp(),result=p_result where request_id=p_request_id;
 get diagnostics n=row_count; return n=1;
end $$;

create function public.amem_model_test_status() returns jsonb
language sql stable security definer set search_path=pg_catalog,pg_temp as $$
 select coalesce(jsonb_agg(jsonb_build_object('provider',provider,'request_id',request_id,
 'requested_at',requested_at,'finished_at',finished_at,'expires_at',finished_at+interval '15 minutes',
 'retry_at',requested_at+interval '5 minutes','state',case
 when result is not null and finished_at<now()-interval '15 minutes' then 'stale'
 when result is not null then case when result->>'code'='ok' then 'verified' else 'failed' end
 when requested_at<now()-interval '3 minutes' then 'unknown'
 when claimed_at is null then 'queued' else 'running' end,'result',result)), '[]'::jsonb)
 from amem_private.model_connection_tests;
$$;
revoke all on function public.amem_model_test_start(text,uuid) from public;
do $$ declare r text; begin
 foreach r in array array['anon','authenticated','amem_app','rsi_reader','rsi_writer','service_role'] loop
 if exists(select 1 from pg_roles where rolname=r) then execute format('revoke all on function public.amem_model_test_start(text,uuid) from %I',r); end if; end loop;
 if exists(select 1 from pg_roles where rolname='service_role') then grant execute on function public.amem_model_test_start(text,uuid) to service_role; end if; end $$;
revoke all on function public.amem_model_test_claim() from public;
do $$ declare r text; begin
 foreach r in array array['anon','authenticated','amem_app','rsi_reader','rsi_writer','service_role'] loop
 if exists(select 1 from pg_roles where rolname=r) then execute format('revoke all on function public.amem_model_test_claim() from %I',r); end if; end loop;
 if exists(select 1 from pg_roles where rolname='amem_app') then grant execute on function public.amem_model_test_claim() to amem_app; end if; end $$;
revoke all on function public.amem_model_test_finish(uuid,uuid,jsonb) from public;
do $$ declare r text; begin
 foreach r in array array['anon','authenticated','amem_app','rsi_reader','rsi_writer','service_role'] loop
 if exists(select 1 from pg_roles where rolname=r) then execute format('revoke all on function public.amem_model_test_finish(uuid,uuid,jsonb) from %I',r); end if; end loop;
 if exists(select 1 from pg_roles where rolname='amem_app') then grant execute on function public.amem_model_test_finish(uuid,uuid,jsonb) to amem_app; end if; end $$;
revoke all on function public.amem_model_test_status() from public;
do $$ declare r text; begin
 foreach r in array array['anon','authenticated','amem_app','rsi_reader','rsi_writer','service_role'] loop
 if exists(select 1 from pg_roles where rolname=r) then execute format('revoke all on function public.amem_model_test_status() from %I',r); end if; end loop;
 if exists(select 1 from pg_roles where rolname='service_role') then grant execute on function public.amem_model_test_status() to service_role; end if; end $$;
notify pgrst,'reload schema';
