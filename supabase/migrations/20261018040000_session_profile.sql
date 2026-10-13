-- Session profile columns (docs/sessions-live.md: "Title", "Project and category",
-- "Model, effort, duration", "Outcome") and the `session_groups` view. Filled by
-- `amem sessions ingest` (all tools, cloud digests too; existing rows on the next ingest)
-- and `amem sessions ping`. Null means "not known": nothing is guessed.
--
--   title, title_updated_at        the chat's name (Claude Code custom-title, the last one wins;
--                                  Codex session_index.jsonl). Redacted, max 200 chars.
--   category                       routine | chat | parent | worker | eval | headless
--   project                        where the work happened, normalized (main repo of a worktree,
--                                  `scratch`, `evals`, `routine:<name>`, ...); never empty
--   model, model_last              main model (most output tokens), the latest reply's model
--   effort, effort_last, efforts   main effort (most replies), the latest reply's, {level: replies}
--   duration_seconds               wall clock: first to last transcript event
--   active_seconds                 sum of gaps between events, gaps over 10 minutes left out
--   api_seconds, tool_seconds      Claude Code cost-state (ms in the transcript), else null
--   lines_added, lines_removed     Claude Code cost-state, else null
--   outcome, outcome_evidence,     shipped | pushed | committed | partial | active | abandoned |
--   outcome_at                     failed | no-change; evidence = signal names and counts only;
--                                  outcome_at = when the outcome last changed
-- RLS on `sessions` is unchanged (on, no policies, no API-role grants).
alter table public.sessions
    add column if not exists title text check (title is null or char_length(title) <= 200),
    add column if not exists title_updated_at timestamptz,
    add column if not exists category text,
    add column if not exists project text,
    add column if not exists model text,
    add column if not exists model_last text,
    add column if not exists effort text,
    add column if not exists effort_last text,
    add column if not exists efforts jsonb,
    add column if not exists duration_seconds double precision,
    add column if not exists active_seconds double precision,
    add column if not exists api_seconds double precision,
    add column if not exists tool_seconds double precision,
    add column if not exists lines_added integer,
    add column if not exists lines_removed integer,
    add column if not exists outcome text,
    add column if not exists outcome_evidence jsonb,
    add column if not exists outcome_at timestamptz;

alter table public.sessions drop constraint if exists sessions_category_check;
alter table public.sessions add constraint sessions_category_check
    check (category is null or category in ('routine', 'chat', 'parent', 'worker', 'eval', 'headless'));
alter table public.sessions drop constraint if exists sessions_outcome_check;
alter table public.sessions add constraint sessions_outcome_check
    check (outcome is null or outcome in ('shipped', 'pushed', 'committed', 'partial', 'active',
                                          'abandoned', 'failed', 'no-change'));

create index if not exists sessions_category_project_idx on public.sessions (category, project);

-- One row per category + project: sessions, token sums, time (sum and median), the latest
-- activity, and session counts per model, effort and outcome. security_invoker: the view
-- reads `sessions` with the caller's rights, so RLS on the table still applies.
drop view if exists public.session_groups;
create view public.session_groups with (security_invoker = true) as
with base as (
    select coalesce(category, 'unknown') as category, coalesce(project, 'unknown') as project,
           model, effort, outcome,
           tokens_input, tokens_output, tokens_cache_read, tokens_cache_creation,
           duration_seconds, active_seconds,
           coalesce(last_activity_at, ended_at, started_at, created_at) as active_at
    from public.sessions
),
counts as (
    select category, project, 'model' as dim, model as value, count(*) as n from base where model is not null
    group by category, project, model
    union all
    select category, project, 'effort', effort, count(*) from base where effort is not null
    group by category, project, effort
    union all
    select category, project, 'outcome', outcome, count(*) from base where outcome is not null
    group by category, project, outcome
),
by_dim as (
    select category, project,
           jsonb_object_agg(value, n) filter (where dim = 'model') as models,
           jsonb_object_agg(value, n) filter (where dim = 'effort') as efforts,
           jsonb_object_agg(value, n) filter (where dim = 'outcome') as outcomes
    from counts group by category, project
)
select b.category, b.project,
       count(*) as sessions,
       coalesce(sum(b.tokens_input), 0) as tokens_input,
       coalesce(sum(b.tokens_output), 0) as tokens_output,
       coalesce(sum(b.tokens_cache_read), 0) as tokens_cache_read,
       coalesce(sum(b.tokens_cache_creation), 0) as tokens_cache_creation,
       coalesce(sum(b.tokens_input + b.tokens_output + b.tokens_cache_read + b.tokens_cache_creation), 0)
           as tokens_total,
       sum(b.duration_seconds) as duration_seconds,
       percentile_cont(0.5) within group (order by b.duration_seconds) as duration_median_seconds,
       sum(b.active_seconds) as active_seconds,
       percentile_cont(0.5) within group (order by b.active_seconds) as active_median_seconds,
       max(b.active_at) as last_activity_at,
       coalesce(d.models, '{}'::jsonb) as models,
       coalesce(d.efforts, '{}'::jsonb) as efforts,
       coalesce(d.outcomes, '{}'::jsonb) as outcomes
from base b
left join by_dim d using (category, project)
group by b.category, b.project, d.models, d.efforts, d.outcomes;

do $$
declare r text;
begin
  foreach r in array array['anon', 'authenticated'] loop
    if exists (select 1 from pg_roles where rolname = r) then
      execute format('revoke all on table public.session_groups from %I', r);
    end if;
  end loop;
end $$;
