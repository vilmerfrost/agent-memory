-- `amem lessons distill` (docs/lessons.md): one row per dispatch run it has considered.
-- A run with a row is not considered again (except `--retry-errors` for decision 'error').
--   decision  accept (a lesson was written or merged), reject (Jev gate or writer said skip),
--             error (invalid run, Jev error, writer error or invalid writer output)
--   reason    one line: the gate scores, the writer's skip reason or the error
--   lesson_id the new or merged lesson (accept only)
create table if not exists public.lesson_candidates (
    run_id        uuid primary key references public.runs (id) on delete cascade,
    decision      text not null check (decision in ('accept', 'reject', 'error')),
    reason        text,
    lesson_id     uuid references public.lessons (id) on delete set null,
    considered_at timestamptz not null default now()
);

create index if not exists lesson_candidates_decision_idx on public.lesson_candidates (decision);

-- Same lockdown as the other tables: RLS on with no policies, and no grants for the
-- Supabase API roles. The roles exist on Supabase but not on the plain test Postgres.
alter table public.lesson_candidates enable row level security;

do $$
declare r text;
begin
  foreach r in array array['anon', 'authenticated'] loop
    if exists (select 1 from pg_roles where rolname = r) then
      execute format('revoke all on table public.lesson_candidates from %I', r);
    end if;
  end loop;
end $$;
