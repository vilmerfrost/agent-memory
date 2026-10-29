-- Parent dispatch runs: dispatch -> brief. The parent chat idles while workers run,
-- so a run is measured by explicit `amem dispatch start` / `amem dispatch end` marks.
-- Usage comes from Vitals plan-limit snapshots (account-wide percent, never raw email).
create table if not exists public.dispatch_runs (
 run_id uuid primary key,
 repo text not null check (length(repo) between 1 and 200),
 parent_session_id text check (length(parent_session_id) <= 200),
 parent_tool text not null check (parent_tool in ('claude','codex')),
 dispatched_at timestamptz not null,
 briefed_at timestamptz check (briefed_at is null or briefed_at >= dispatched_at),
 duration_s double precision generated always as (extract(epoch from (briefed_at - dispatched_at))) stored,
 n_workers integer not null check (n_workers >= 0),
 brief_chars integer check (brief_chars >= 0),
 files_claimed integer check (files_claimed >= 0),
 integration_test_ms integer check (integration_test_ms >= 0),
 tokens_fresh bigint check (tokens_fresh >= 0),
 tokens_cache_read bigint check (tokens_cache_read >= 0),
 concurrent_sessions integer check (concurrent_sessions >= 0),
 status text not null default 'open' check (status in ('open','done','partial','blocked','abandoned','failed')),
 meta jsonb not null default '{}'::jsonb check (jsonb_typeof(meta)='object'),
 created_at timestamptz not null default now(),
 updated_at timestamptz not null default now()
);
create index if not exists dispatch_runs_tool_time_idx on public.dispatch_runs(parent_tool,dispatched_at desc);
create index if not exists dispatch_runs_session_idx on public.dispatch_runs(parent_session_id) where parent_session_id is not null;

create table if not exists public.usage_snapshots (
 snapshot_id uuid primary key,
 run_id uuid not null references public.dispatch_runs(run_id) on delete cascade,
 phase text not null check (phase in ('start','end')),
 taken_at timestamptz not null,
 tool text not null check (length(tool) between 1 and 40),
 account_hash text not null check (account_hash ~ '^[0-9a-f]{16,64}$'),
 limit_kind text not null check (length(limit_kind) between 1 and 40),
 limit_name text not null check (length(limit_name) between 1 and 120),
 used_percent numeric not null check (used_percent >= 0 and used_percent <= 1000),
 resets_at timestamptz,
 source text not null default 'vitals' check (length(source) between 1 and 40),
 unique (run_id,phase,tool,account_hash,limit_name)
);
create index if not exists usage_snapshots_run_idx on public.usage_snapshots(run_id,phase);

create table if not exists public.dispatch_estimates (
 run_id uuid primary key references public.dispatch_runs(run_id) on delete cascade,
 made_at timestamptz not null,
 est_minutes integer check (est_minutes >= 0),
 est_slow_minutes integer check (est_slow_minutes >= 0),
 est_usage jsonb not null default '{}'::jsonb check (jsonb_typeof(est_usage)='object'),
 basis_runs integer not null default 0 check (basis_runs >= 0),
 method_version text not null check (length(method_version) between 1 and 60)
);

alter table public.dispatch_runs enable row level security;
alter table public.usage_snapshots enable row level security;
alter table public.dispatch_estimates enable row level security;
revoke all on public.dispatch_runs,public.usage_snapshots,public.dispatch_estimates from public;

-- Actuals per run and limit. A usage delta is clean only when no other session of the
-- same tool was working (meta.concurrent_by_tool, else concurrent_sessions = 0: plan
-- limits are per tool account) and the limit did not reset in between (resets_at moved
-- by at most 5 minutes: Vitals rounds rolling windows).
create or replace view public.dispatch_run_actuals with (security_invoker=true) as
select r.run_id,r.repo,r.parent_tool,r.parent_session_id,r.n_workers,r.status,
       r.dispatched_at,r.briefed_at,r.duration_s,r.integration_test_ms,
       r.tokens_fresh,r.tokens_cache_read,r.concurrent_sessions,
       s.tool as limit_tool,s.limit_kind,s.limit_name,
       s.used_percent as start_percent,e.used_percent as end_percent,
       e.used_percent-s.used_percent as usage_delta,
       (s.resets_at is not distinct from e.resets_at
        or abs(extract(epoch from (e.resets_at-s.resets_at))) <= 300) as same_window,
       (coalesce((r.meta->'concurrent_by_tool'->>s.tool)::int, r.concurrent_sessions) = 0
        and (s.resets_at is not distinct from e.resets_at
             or abs(extract(epoch from (e.resets_at-s.resets_at))) <= 300)) as clean,
       d.est_minutes,d.est_slow_minutes,d.est_usage,d.basis_runs
from public.dispatch_runs r
left join public.usage_snapshots s on s.run_id=r.run_id and s.phase='start'
left join public.usage_snapshots e on e.run_id=r.run_id and e.phase='end'
  and e.tool=s.tool and e.account_hash=s.account_hash and e.limit_name=s.limit_name
left join public.dispatch_estimates d on d.run_id=r.run_id;

do $$
begin
 if exists(select 1 from pg_roles where rolname='amem_app') then
   grant select,insert,update on public.dispatch_runs,public.usage_snapshots,public.dispatch_estimates to amem_app;
   drop policy if exists dispatch_runs_ingest on public.dispatch_runs;
   create policy dispatch_runs_ingest on public.dispatch_runs for all to amem_app using(true) with check(true);
   drop policy if exists usage_snapshots_ingest on public.usage_snapshots;
   create policy usage_snapshots_ingest on public.usage_snapshots for all to amem_app using(true) with check(true);
   drop policy if exists dispatch_estimates_ingest on public.dispatch_estimates;
   create policy dispatch_estimates_ingest on public.dispatch_estimates for all to amem_app using(true) with check(true);
   grant select on public.dispatch_run_actuals to amem_app;
 end if;
 if exists(select 1 from pg_roles where rolname='anon') then
   revoke all on public.dispatch_runs,public.usage_snapshots,public.dispatch_estimates,public.dispatch_run_actuals from anon;
 end if;
 if exists(select 1 from pg_roles where rolname='authenticated') and to_regprocedure('auth.uid()') is not null then
   revoke all on public.dispatch_runs,public.usage_snapshots,public.dispatch_estimates from authenticated;
   grant select on public.dispatch_runs,public.usage_snapshots,public.dispatch_estimates,public.dispatch_run_actuals to authenticated;
   drop policy if exists dispatch_runs_owner_read on public.dispatch_runs;
   create policy dispatch_runs_owner_read on public.dispatch_runs for select to authenticated
     using ((select auth.uid())=(select public.amem_owner_uid()));
   drop policy if exists usage_snapshots_owner_read on public.usage_snapshots;
   create policy usage_snapshots_owner_read on public.usage_snapshots for select to authenticated
     using ((select auth.uid())=(select public.amem_owner_uid()));
   drop policy if exists dispatch_estimates_owner_read on public.dispatch_estimates;
   create policy dispatch_estimates_owner_read on public.dispatch_estimates for select to authenticated
     using ((select auth.uid())=(select public.amem_owner_uid()));
 end if;
end $$;
notify pgrst,'reload schema';
