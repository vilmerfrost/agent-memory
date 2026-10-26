-- Summaries survive retention; case rows contain measurements, never logs.
create table public.test_timing_runs (
 run_id uuid primary key, session_id text, parent_span_id text,
 repo text not null, head text, dirty boolean,
 selection text not null check(selection in ('full','custom')),
 workers integer not null check(workers >= 0),
 started_at timestamptz not null,
 ended_at timestamptz not null check(ended_at >= started_at),
 elapsed_ms double precision not null check(elapsed_ms >= 0 and elapsed_ms < 'Infinity'::float8),
 exit_code integer not null,
 collection_ready_ms double precision check(collection_ready_ms >= 0 and collection_ready_ms < 'Infinity'::float8),
 collection_errors integer not null check(collection_errors >= 0),
 worker_errors integer not null check(worker_errors >= 0),
 tests_reported integer not null check(tests_reported >= 0),
 tests_complete integer not null check(tests_complete between 0 and tests_reported),
 outcomes jsonb not null check(jsonb_typeof(outcomes)='object'),
 log_ref text not null, details_retained boolean not null default true,
 created_at timestamptz not null default now()
);
create index test_timing_runs_session_idx on public.test_timing_runs(session_id,ended_at desc);
create index test_timing_runs_repo_idx on public.test_timing_runs(repo,ended_at desc);
create index test_timing_runs_retention_idx on public.test_timing_runs(ended_at) where details_retained;
create table public.test_timing_cases (
 run_id uuid not null references public.test_timing_runs(run_id),
 test_key text not null check(length(test_key)=64),
 nodeid text not null check(length(nodeid) between 1 and 2048), worker_id text,
 setup_ms double precision check(setup_ms >= 0 and setup_ms < 'Infinity'::float8),
 call_ms double precision check(call_ms >= 0 and call_ms < 'Infinity'::float8),
 teardown_ms double precision check(teardown_ms >= 0 and teardown_ms < 'Infinity'::float8),
 total_ms double precision check(total_ms >= 0 and total_ms < 'Infinity'::float8),
 outcome text not null check(outcome in ('passed','failed','error','skipped','xfailed','xpassed','incomplete')),
 complete boolean not null, primary key(run_id,test_key)
);
create index test_timing_cases_slowest_idx on public.test_timing_cases(run_id,total_ms desc);
create index test_timing_cases_history_idx on public.test_timing_cases(test_key,run_id);
alter table public.test_timing_runs enable row level security;
alter table public.test_timing_cases enable row level security;
revoke all on public.test_timing_runs,public.test_timing_cases from public;
do $$
begin
 if exists(select 1 from pg_roles where rolname='amem_app') then
   grant select,insert on public.test_timing_runs,public.test_timing_cases to amem_app;
   grant update(details_retained) on public.test_timing_runs to amem_app;
   grant delete on public.test_timing_cases to amem_app;
   create policy test_timing_runs_read on public.test_timing_runs for select to amem_app using(true);
   create policy test_timing_runs_insert on public.test_timing_runs for insert to amem_app with check(true);
   create policy test_timing_runs_retention on public.test_timing_runs for update to amem_app
     using(ended_at < now()-interval '30 days') with check(not details_retained);
   create policy test_timing_cases_read on public.test_timing_cases for select to amem_app using(true);
   create policy test_timing_cases_insert on public.test_timing_cases for insert to amem_app with check(true);
   create policy test_timing_cases_retention on public.test_timing_cases for delete to amem_app
     using(exists(select 1 from public.test_timing_runs r where r.run_id=test_timing_cases.run_id
                  and r.ended_at < now()-interval '30 days'));
 end if;
 if exists(select 1 from pg_roles where rolname='anon') then
   revoke all on public.test_timing_runs,public.test_timing_cases from anon;
 end if;
 if exists(select 1 from pg_roles where rolname='authenticated') and to_regprocedure('auth.uid()') is not null then
   revoke all on public.test_timing_runs,public.test_timing_cases from authenticated;
   grant select on public.test_timing_runs,public.test_timing_cases to authenticated;
   create policy test_timing_runs_owner on public.test_timing_runs for select to authenticated
     using((select auth.uid())=(select public.amem_owner_uid()));
   create policy test_timing_cases_owner on public.test_timing_cases for select to authenticated
     using((select auth.uid())=(select public.amem_owner_uid()));
 end if;
end $$;
notify pgrst,'reload schema';
