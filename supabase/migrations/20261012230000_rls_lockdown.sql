-- Lock the tables away from Supabase's Data API (anon/authenticated roles).
-- RLS on with no policies: API roles get nothing; amem connects as the owner and is unaffected.
alter table public.runs enable row level security;
alter table public.schema_migrations enable row level security;

-- The roles exist on Supabase but not on the plain test Postgres.
do $$
declare r text;
begin
  foreach r in array array['anon', 'authenticated'] loop
    if exists (select 1 from pg_roles where rolname = r) then
      execute format('revoke all on table public.runs from %I', r);
      execute format('revoke all on table public.schema_migrations from %I', r);
    end if;
  end loop;
end $$;
