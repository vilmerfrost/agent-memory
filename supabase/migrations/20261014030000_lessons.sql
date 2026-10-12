-- Lessons learned from runs, and a log of every time one was injected into a brief.
-- Full-text search (tsv) is the V1 retrieval. `embedding` exists for later; V1 never fills it.

-- pgvector: create it only when the server ships it. On Supabase it belongs in
-- schema `extensions`; on plain Postgres (the test image) the default schema is fine.
do $$
begin
  if not exists (select 1 from pg_extension where extname = 'vector')
     and exists (select 1 from pg_available_extensions where name = 'vector') then
    if exists (select 1 from pg_namespace where nspname = 'extensions') then
      execute 'create extension if not exists vector with schema extensions';
    else
      execute 'create extension if not exists vector';
    end if;
  end if;
end $$;

create table if not exists public.lessons (
    id             uuid primary key default gen_random_uuid(),
    slug           text not null unique,               -- the lessons.md `Id:`
    title          text not null,
    rule           text not null,                      -- what a worker should do; = title for imported lessons
    scope_repo     text,                               -- null = every repo
    scope_task     text,                               -- null = every task; else a phrase the task text must contain
    status         text not null default 'trial'
                   check (status in ('trial', 'kept', 'retired', 'promoted')),
    source_run_ids uuid[] not null default '{}',
    evidence       text,
    evals          text,                               -- the lessons.md `Evals:` (kept for round-trip)
    promoted_to    text,                               -- the lessons.md `Promoted-to:`
    since          date,
    tsv            tsvector generated always as (
                       setweight(to_tsvector('english'::regconfig, coalesce(title, '')), 'A')
                    || setweight(to_tsvector('english'::regconfig, coalesce(rule, '')), 'B')
                    || setweight(to_tsvector('english'::regconfig, coalesce(evidence, '')), 'C')
                   ) stored,
    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now()
);

create index if not exists lessons_tsv_idx on public.lessons using gin (tsv);
create index if not exists lessons_scope_status_idx on public.lessons (scope_repo, status);

-- embedding vector(1536), only when pgvector is installed. The type is schema-qualified
-- because on Supabase it lives in `extensions`, which may not be on the search_path.
do $$
declare nsp text;
begin
  select n.nspname into nsp
    from pg_type t join pg_namespace n on n.oid = t.typnamespace
   where t.typname = 'vector'
   limit 1;
  if nsp is not null then
    execute format('alter table public.lessons add column if not exists embedding %I.vector(1536)', nsp);
  end if;
end $$;

create table if not exists public.lesson_uses (
    id          uuid primary key default gen_random_uuid(),
    lesson_id   uuid not null references public.lessons (id) on delete cascade,
    run_id      uuid references public.runs (id) on delete set null,
    injected_at timestamptz not null default now(),
    context     text,                                  -- the task text the lesson was injected for
    outcome     text check (outcome is null or outcome in ('pass', 'fail', 'unknown'))
);

create index if not exists lesson_uses_lesson_idx on public.lesson_uses (lesson_id);
create index if not exists lesson_uses_run_idx on public.lesson_uses (run_id);

-- Same lockdown as runs: RLS on with no policies, and no table grants for the
-- Supabase API roles. The roles exist on Supabase but not on the plain test Postgres.
alter table public.lessons enable row level security;
alter table public.lesson_uses enable row level security;

do $$
declare r text;
begin
  foreach r in array array['anon', 'authenticated'] loop
    if exists (select 1 from pg_roles where rolname = r) then
      execute format('revoke all on table public.lessons from %I', r);
      execute format('revoke all on table public.lesson_uses from %I', r);
    end if;
  end loop;
end $$;
