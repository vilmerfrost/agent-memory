-- Handoffs (docs/handoffs.md): one row per session handoff, written by `amem handoffs log`
-- (a Stop hook for `auto`, the handoff skill for `manual`). The sequel chat marks the row
-- with `amem handoffs resume`. An event source for the RSI loop: `amem evals harvest` reads
-- session_id, project, handoff_path, reason and created_at. Keep these names stable.
--
--   session_id          the session that handed off (sessions.session_id; no FK: the
--                       session may not be ingested yet)
--   project             repo or project name (sessions.project style), null when unknown
--   handoff_path        the handoff file the sequel reads
--   reason              auto (context limit / hook) | manual (asked for)
--   context_tokens      context size at handoff time, null when unknown
--   context_pct         context use 0..100 at handoff time, null when unknown
--   title               the sequel chat's title, null when none
--   resumed_session_id  the session that resumed from this handoff (null: not resumed)
--   resumed_at          when it resumed
create table if not exists public.handoffs (
    id                 uuid primary key default gen_random_uuid(),
    session_id         text not null,
    project            text,
    handoff_path       text not null,
    reason             text not null default 'manual' check (reason in ('auto', 'manual')),
    context_tokens     int check (context_tokens is null or context_tokens >= 0),
    context_pct        real check (context_pct is null or (context_pct >= 0 and context_pct <= 100)),
    title              text,
    created_at         timestamptz not null default now(),
    resumed_session_id text,
    resumed_at         timestamptz
);

create index if not exists handoffs_created_idx on public.handoffs (created_at desc);
create index if not exists handoffs_session_idx on public.handoffs (session_id);

-- Same lockdown as the other tables: RLS on with no policies, and no grants for the
-- Supabase API roles. The roles exist on Supabase but not on the plain test Postgres.
alter table public.handoffs enable row level security;

do $$
declare r text;
begin
  foreach r in array array['anon', 'authenticated'] loop
    if exists (select 1 from pg_roles where rolname = r) then
      execute format('revoke all on table public.handoffs from %I', r);
    end if;
  end loop;
end $$;
