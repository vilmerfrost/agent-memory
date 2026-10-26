-- Observed operations, separate from estimated session activity. Events may arrive
-- before sessions/parents, so correlation IDs intentionally have no immediate FK.
create table if not exists public.execution_spans (
 span_id text primary key check (length(span_id) between 1 and 160),
 session_id text,
 parent_span_id text check (parent_span_id is distinct from span_id),
 tool text not null check (tool in ('claude','codex','unknown')),
 category text not null check (category in ('workflow','setup','test_full','test_targeted','command','tool','api','model_output','review_wait','approval_wait','unclassified')),
 operation text not null check (length(operation) between 1 and 100),
 started_at timestamptz,
 ended_at timestamptz,
 elapsed_ms double precision check (elapsed_ms >= 0 and elapsed_ms < 'Infinity'::double precision),
 status text not null check (status in ('running','passed','failed','error','interrupted','unknown')),
 exit_code integer,
 measurement_kind text not null check (measurement_kind in ('measured','provider_reported','observed','estimated','unknown')),
 source text not null,
 method_version integer not null default 1 check (method_version > 0),
 meta jsonb not null default '{}'::jsonb check (jsonb_typeof(meta)='object'),
 created_at timestamptz not null default now(),
 updated_at timestamptz not null default now(),
 check (started_at is null or ended_at is null or ended_at >= started_at)
);
create index if not exists execution_spans_session_time_idx on public.execution_spans(session_id,started_at);
create index if not exists execution_spans_parent_idx on public.execution_spans(parent_span_id) where parent_span_id is not null;
create index if not exists execution_spans_time_category_idx on public.execution_spans(started_at desc,category);
alter table public.execution_spans enable row level security;

-- Preserve metadata as the source of truth, while allowing safe, indexed parent
-- joins without granting the browser access to the rest of meta.
alter table public.sessions add column if not exists parent_session_id text
 generated always as (nullif(meta->>'parent_session','')) stored;
create index if not exists sessions_parent_session_idx on public.sessions(parent_session_id) where parent_session_id is not null;

-- Multirange unions count overlapping/nested intervals once. Category unions can
-- overlap each other; they must never be added to get the session wall time.
create or replace view public.session_timing_summary with (security_invoker=true) as
with bounds as (
 select s.session_id,s.tool,s.category,s.project,s.parent_session_id,s.duration_seconds,s.active_seconds,
        s.api_seconds,s.tool_seconds,
        coalesce(s.started_at,min(e.started_at)) as window_start,
        greatest(s.ended_at,max(e.ended_at)) as window_end
 from public.sessions s left join public.execution_spans e on e.session_id=s.session_id
   and e.measurement_kind in ('measured','provider_reported','observed')
 group by s.session_id
), valid as (
 select e.*,tstzrange(greatest(e.started_at,b.window_start),least(e.ended_at,b.window_end),'[)') as period
 from public.execution_spans e join bounds b using(session_id)
 where e.started_at is not null and e.ended_at is not null
   and e.category <> 'workflow'
   and e.measurement_kind in ('measured','provider_reported','observed')
   and greatest(e.started_at,b.window_start) <= least(e.ended_at,b.window_end)
), merged as (
 select session_id,range_agg(period) as periods from valid group by session_id
), observed as (
 select session_id,coalesce((select sum(extract(epoch from upper(p)-lower(p))) from unnest(periods) p),0) as seconds
 from merged
), categories as (
 select session_id,category,range_agg(period) as periods from valid group by session_id,category
), category_seconds as (
 select session_id,jsonb_object_agg(category,coalesce((select sum(extract(epoch from upper(p)-lower(p))) from unnest(periods) p),0)) as seconds
 from categories group by session_id
), counts as (
 select session_id,count(*) as spans,
        count(*) filter(where status='running') as unfinished_spans,
        count(*) filter(where measurement_kind='estimated') as estimated_spans,
        count(*) filter(where measurement_kind='unknown') as unknown_spans,
        count(*) filter(where category='api' and elapsed_ms is not null and measurement_kind in ('measured','provider_reported','observed')) as api_measured_spans,
        sum(elapsed_ms/1000) filter(where category='api' and measurement_kind in ('measured','provider_reported','observed')) as api_measured_seconds,
        count(*) filter(where source in ('codex.item','claude.tool') and category in ('tool','command','test_full','test_targeted') and elapsed_ms is not null and measurement_kind in ('measured','provider_reported','observed')) as tool_measured_spans,
        sum(elapsed_ms/1000) filter(where source in ('codex.item','claude.tool') and category in ('tool','command','test_full','test_targeted') and measurement_kind in ('measured','provider_reported','observed')) as tool_measured_seconds
 from public.execution_spans group by session_id
)
select b.*,extract(epoch from window_end-window_start) as window_seconds,
       o.seconds as observed_wall_seconds,
       case when window_start is not null and window_end is not null
         then greatest(extract(epoch from window_end-window_start)-coalesce(o.seconds,0),0) end as unclassified_seconds,
       coalesce(c.seconds,'{}'::jsonb) as category_union_seconds,
       coalesce(n.spans,0) as spans,coalesce(n.unfinished_spans,0) as unfinished_spans,
       coalesce(n.estimated_spans,0) as estimated_spans,coalesce(n.unknown_spans,0) as unknown_spans,
       coalesce(n.api_measured_spans,0) as api_measured_spans,n.api_measured_seconds,
       coalesce(n.tool_measured_spans,0) as tool_measured_spans,n.tool_measured_seconds,
       true as category_times_may_overlap
from bounds b left join observed o using(session_id)
left join category_seconds c using(session_id) left join counts n using(session_id);

create or replace view public.session_parent_timing with (security_invoker=true) as
with recursive family(root_session_id,session_id) as (
 select session_id,session_id from public.sessions
 union
 select f.root_session_id,s.session_id from family f join public.sessions s on s.parent_session_id=f.session_id
), children as (
 select f.root_session_id,count(*) as descendant_sessions,
        count(*) filter(where s.category='worker') as worker_sessions,
        sum(s.duration_seconds) as cumulative_descendant_elapsed_seconds,
        count(s.duration_seconds) as descendants_with_elapsed
 from family f join public.sessions s on s.session_id=f.session_id
 where f.session_id<>f.root_session_id group by f.root_session_id
)
select t.*,coalesce(c.descendant_sessions,0) as descendant_sessions,
       coalesce(c.worker_sessions,0) as worker_sessions,c.cumulative_descendant_elapsed_seconds,
       coalesce(c.descendants_with_elapsed,0) as descendants_with_elapsed
from public.session_timing_summary t left join children c on c.root_session_id=t.session_id;

create or replace view public.session_timing_coverage with (security_invoker=true) as
select tool,count(*) as sessions,count(duration_seconds) as with_elapsed,
       count(active_seconds) as with_activity_estimate,count(api_seconds) as with_api_time,
       count(tool_seconds) as with_tool_time,count(*) filter(where parent_session_id is not null) as with_parent
from public.sessions group by tool;

do $$
begin
 if exists(select 1 from pg_roles where rolname='amem_app') then
   grant select,insert,update on public.execution_spans to amem_app;
   drop policy if exists execution_spans_ingest on public.execution_spans;
   create policy execution_spans_ingest on public.execution_spans for all to amem_app using(true) with check(true);
   grant select on public.session_timing_summary,public.session_parent_timing,public.session_timing_coverage to amem_app;
 end if;
 if exists(select 1 from pg_roles where rolname='anon') then
   revoke all on public.execution_spans,public.session_timing_summary,public.session_parent_timing,public.session_timing_coverage from anon;
 end if;
 if exists(select 1 from pg_roles where rolname='authenticated') and to_regprocedure('auth.uid()') is not null then
   revoke all on public.execution_spans from authenticated;
   grant select on public.execution_spans to authenticated;
   drop policy if exists execution_spans_owner_read on public.execution_spans;
   create policy execution_spans_owner_read on public.execution_spans for select to authenticated
     using ((select auth.uid())=(select public.amem_owner_uid()));
   grant select(session_id,tool,category,project,parent_session_id,started_at,ended_at,duration_seconds,active_seconds,api_seconds,tool_seconds)
     on public.sessions to authenticated;
   grant select on public.session_timing_summary,public.session_parent_timing,public.session_timing_coverage to authenticated;
 end if;
end $$;
notify pgrst,'reload schema';
