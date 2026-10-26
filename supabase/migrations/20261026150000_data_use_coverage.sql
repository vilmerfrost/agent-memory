-- Data-use coverage contract (plan section 4, Stage B; docs/data-use/coverage.md). Two compact tables:
--
-- coverage_snapshots: one row per dataset and time window (natural key). What was eligible, collected, linked
--   and measured, with an explicit denominator. Recording the same window again updates the row.
-- routine_consumption: one row per job run and dataset (natural key job + run_key + dataset). What the job
--   queried, considered, used and skipped, and the findings it produced, with at most 20 evidence refs.
--   job + run_key is the same key rsi_checkpoints uses; run_id links the existing runs row when there is one.
--
-- status is a fixed vocabulary, never free text. The eight required distinctions:
--   snapshot:     no_eligible_data | not_collected | collected_unlinked | measurement_unavailable
--   consumption:  read_excluded | used_no_finding | analysis_not_run | analysis_failed
-- plus `complete` (snapshot: everything eligible collected, linked and measured) and `findings` (consumption).
-- Checks keep each status consistent with its counts.
--
-- Access: RLS on. The routine role (amem_app) reads, inserts and updates (no delete); Night Desk's owner reads;
-- no other role (anon, authenticated non-owner, service_role, rsi_reader) gets anything.

create table if not exists public.coverage_snapshots (
    id                uuid primary key default gen_random_uuid(),
    dataset           text not null check (dataset ~ '^[a-z][a-z0-9_.:-]{0,79}$'),
    window_start      timestamptz not null,
    window_end        timestamptz not null,
    denominator       text not null check (length(denominator) between 1 and 300),
    eligible          bigint check (eligible is null or eligible >= 0),
    collected         bigint check (collected is null or collected >= 0),
    linked            bigint check (linked is null or linked >= 0),
    unlinked          bigint check (unlinked is null or unlinked >= 0),
    pending           bigint not null default 0 check (pending >= 0),
    rejected          bigint not null default 0 check (rejected >= 0),
    expired           bigint not null default 0 check (expired >= 0),
    measured_fields   text[] not null default '{}' check (cardinality(measured_fields) <= 50),
    unmeasured_fields text[] not null default '{}' check (cardinality(unmeasured_fields) <= 50),
    last_collected_at timestamptz,
    last_analysed_at  timestamptz,
    source_versions   jsonb not null default '{}'::jsonb
                      check (jsonb_typeof(source_versions) = 'object' and octet_length(source_versions::text) <= 4000),
    checkpoints       jsonb not null default '{}'::jsonb
                      check (jsonb_typeof(checkpoints) = 'object' and octet_length(checkpoints::text) <= 4000),
    status            text not null check (status in ('complete', 'no_eligible_data', 'not_collected',
                                                      'collected_unlinked', 'measurement_unavailable')),
    detail            text check (detail is null or length(detail) <= 500),
    run_id            uuid references public.runs (id) on delete set null,
    recorded_at       timestamptz not null default now(),
    updated_at        timestamptz not null default now(),
    constraint coverage_snapshots_window_check check (window_end >= window_start),
    constraint coverage_snapshots_counts_check check (
        (eligible is null or collected is null or collected <= eligible)
        and (collected is null or coalesce(linked, 0) + coalesce(unlinked, 0) <= collected)),
    constraint coverage_snapshots_status_check2 check (coalesce(case status
        when 'complete' then eligible is not null and collected = eligible and coalesce(unlinked, 0) = 0
                             and cardinality(unmeasured_fields) = 0
        when 'no_eligible_data' then eligible = 0
        when 'not_collected' then eligible > 0 and coalesce(collected, 0) < eligible
        when 'collected_unlinked' then unlinked > 0
        when 'measurement_unavailable' then eligible is null or cardinality(unmeasured_fields) > 0
        end, false)),
    constraint coverage_snapshots_natural_key unique (dataset, window_start, window_end)
);
create index if not exists coverage_snapshots_dataset_idx on public.coverage_snapshots (dataset, window_end desc);

create table if not exists public.routine_consumption (
    id                uuid primary key default gen_random_uuid(),
    job               text not null check (job ~ '^[a-z][a-z0-9_.:-]{0,79}$'),
    run_key           text not null check (length(run_key) between 1 and 120),
    run_id            uuid references public.runs (id) on delete set null,
    dataset           text not null check (dataset ~ '^[a-z][a-z0-9_.:-]{0,79}$'),
    analysis_version  text not null check (length(analysis_version) between 1 and 80),
    window_start      timestamptz,
    window_end        timestamptz,
    source_checkpoint text check (source_checkpoint is null or length(source_checkpoint) <= 300),
    queried           bigint not null default 0 check (queried >= 0),
    considered        bigint not null default 0 check (considered >= 0),
    used              bigint not null default 0 check (used >= 0),
    skipped           jsonb not null default '{}'::jsonb
                      check (jsonb_typeof(skipped) = 'object' and octet_length(skipped::text) <= 2000),
    findings          integer not null default 0 check (findings >= 0),
    evidence_refs     text[] not null default '{}' check (cardinality(evidence_refs) <= 20),
    status            text not null check (status in ('findings', 'used_no_finding', 'read_excluded',
                                                      'analysis_not_run', 'analysis_failed')),
    detail            text check (detail is null or length(detail) <= 500),
    recorded_at       timestamptz not null default now(),
    updated_at        timestamptz not null default now(),
    constraint routine_consumption_window_check check (
        (window_start is null) = (window_end is null) and (window_end is null or window_end >= window_start)),
    constraint routine_consumption_funnel_check check (used <= considered and considered <= queried),
    constraint routine_consumption_status_check2 check (coalesce(case status
        when 'findings' then findings > 0 and used > 0
        when 'used_no_finding' then findings = 0 and used > 0
        when 'read_excluded' then findings = 0 and used = 0 and queried > 0
        when 'analysis_not_run' then findings = 0 and used = 0 and considered = 0
        when 'analysis_failed' then findings = 0
        end, false)),
    constraint routine_consumption_natural_key unique (job, run_key, dataset)
);
create index if not exists routine_consumption_dataset_idx on public.routine_consumption (dataset, recorded_at desc);
create index if not exists routine_consumption_run_idx on public.routine_consumption (run_id) where run_id is not null;

alter table public.coverage_snapshots enable row level security;
alter table public.routine_consumption enable row level security;
revoke all on public.coverage_snapshots, public.routine_consumption from public;

do $$
declare r text;
begin
  foreach r in array array['anon', 'authenticated', 'service_role', 'rsi_reader', 'rsi_writer'] loop
    if exists (select 1 from pg_roles where rolname = r) then
      execute format('revoke all on public.coverage_snapshots, public.routine_consumption from %I', r);
    end if;
  end loop;
  if exists (select 1 from pg_roles where rolname = 'amem_app') then
    grant select, insert, update on public.coverage_snapshots, public.routine_consumption to amem_app;
    drop policy if exists coverage_snapshots_app_read on public.coverage_snapshots;
    create policy coverage_snapshots_app_read on public.coverage_snapshots for select to amem_app using (true);
    drop policy if exists coverage_snapshots_app_insert on public.coverage_snapshots;
    create policy coverage_snapshots_app_insert on public.coverage_snapshots for insert to amem_app with check (true);
    drop policy if exists coverage_snapshots_app_update on public.coverage_snapshots;
    create policy coverage_snapshots_app_update on public.coverage_snapshots for update to amem_app
      using (true) with check (true);
    drop policy if exists routine_consumption_app_read on public.routine_consumption;
    create policy routine_consumption_app_read on public.routine_consumption for select to amem_app using (true);
    drop policy if exists routine_consumption_app_insert on public.routine_consumption;
    create policy routine_consumption_app_insert on public.routine_consumption for insert to amem_app
      with check (true);
    drop policy if exists routine_consumption_app_update on public.routine_consumption;
    create policy routine_consumption_app_update on public.routine_consumption for update to amem_app
      using (true) with check (true);
  end if;
  -- Night Desk: the owner's JWT reads (no writes); any other signed-in user sees no rows.
  if exists (select 1 from pg_roles where rolname = 'authenticated') and to_regprocedure('auth.uid()') is not null
     and to_regprocedure('public.amem_owner_uid()') is not null then
    grant select on public.coverage_snapshots, public.routine_consumption to authenticated;
    drop policy if exists dashboard_owner_read on public.coverage_snapshots;
    create policy dashboard_owner_read on public.coverage_snapshots for select to authenticated
      using ((select auth.uid()) = (select public.amem_owner_uid()));
    drop policy if exists dashboard_owner_read on public.routine_consumption;
    create policy dashboard_owner_read on public.routine_consumption for select to authenticated
      using ((select auth.uid()) = (select public.amem_owner_uid()));
  end if;
end $$;

notify pgrst, 'reload schema';
