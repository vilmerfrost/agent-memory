-- Dispatch tasks (`amem ingest tasks`, docs/tasks.md) land in runs with source = 'dispatch'.
-- Widen the source check. The constraint is the inline column check from
-- 20261012220000_runs.sql, which Postgres names runs_source_check.
alter table public.runs drop constraint if exists runs_source_check;
alter table public.runs add constraint runs_source_check
    check (source is null or source in ('launchd', 'scheduled-task', 'cloud', 'manual', 'dispatch'));

-- No new table or column. Re-assert the lockdown so this file is safe on its own:
-- RLS stays on, and anon/authenticated lose all table rights when those roles
-- exist (Supabase; the plain test Postgres has no such roles).
alter table public.runs enable row level security;

do $$
declare r text;
begin
  foreach r in array array['anon', 'authenticated'] loop
    if exists (select 1 from pg_roles where rolname = r) then
      execute format('revoke all on table public.runs from %I', r);
    end if;
  end loop;
end $$;
