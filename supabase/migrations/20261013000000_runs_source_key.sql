-- Idempotent ingest: a stable key per ingested source line (e.g. routines.log).
-- Nullable: rows from `amem log-run` have no source line and keep source_key null.
-- A plain unique index allows many nulls and backs `on conflict (source_key)`.
alter table public.runs add column if not exists source_key text;

create unique index if not exists runs_source_key_key on public.runs (source_key);

-- No new table: RLS on public.runs stays enabled (20261012230000_rls_lockdown.sql),
-- and its table-level revokes for anon/authenticated already cover the new column.
-- Re-assert both so this file is safe on its own; the revokes stay conditional
-- because the plain test Postgres has no Supabase roles.
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
