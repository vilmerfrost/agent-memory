-- The deviation finder (docs/deviations.md). Read-only toward the setup: these tables
-- describe sessions and where an agent deviated from an instruction. Nothing here edits
-- an instruction; that is `improve apply` in agent-config (later step E4).

-- One row per local Claude Code transcript (`amem sessions ingest`).
--   kind          chat | parent | worker | headless | eval | routine
--   project_dir   the ~/.claude/projects folder name
--   skills        skill names used (Skill tool calls + slash commands)
--   instructions  instruction sources in force, e.g. ["claude-md:global", "skill:dispatch"]
--   scanned_at    set by `amem deviations scan` once the session was read
create table if not exists public.sessions (
    session_id       text primary key,
    kind             text not null check (kind in ('chat', 'parent', 'worker', 'headless', 'eval', 'routine')),
    repo             text,
    project_dir      text not null,
    started_at       timestamptz,
    ended_at         timestamptz,
    transcript_bytes bigint not null default 0,
    skills           jsonb not null default '[]'::jsonb,
    instructions     jsonb not null default '[]'::jsonb,
    meta             jsonb not null default '{}'::jsonb,
    scanned_at       timestamptz,
    created_at       timestamptz not null default now()
);

create index if not exists sessions_unscanned_idx on public.sessions (started_at desc) where scanned_at is null;
create index if not exists sessions_kind_idx on public.sessions (kind);

-- One row per deviation the writer found in a session (`amem deviations scan`).
--   source_key          dev:<session_id>:<hash of source + ref + turn> (idempotent re-scan)
--   instruction_source  skill:<name> | claude-md:global | claude-md:<repo> | parent-md |
--                       routine:<name> | agent:<type>
--   instruction_ref     the heading or rule name inside that source (short)
--   what / why / how    the writer's own words (no transcript quotes, paths, code or secrets)
--   followed_grade      0..1, how well the agent followed the instruction (raw grade 1)
--   result_grade        0..1, how good the result was anyway (raw grade 2)
--   jev_verdict         Jev's call: edit_instruction | enforce | leave (null: not judged)
create table if not exists public.deviations (
    id                 uuid primary key default gen_random_uuid(),
    source_key         text not null unique,
    session_id         text not null references public.sessions (session_id) on delete cascade,
    instruction_source text not null,
    instruction_ref    text not null,
    turn               int,
    what               text not null,
    why                text not null,
    how                text not null,
    followed_grade     real not null check (followed_grade >= 0 and followed_grade <= 1),
    result_grade       real not null check (result_grade >= 0 and result_grade <= 1),
    jev_verdict        text check (jev_verdict is null or jev_verdict in ('edit_instruction', 'enforce', 'leave')),
    jev_confidence     real check (jev_confidence is null or (jev_confidence >= 0 and jev_confidence <= 1)),
    jev_mode           text,
    jev_answers        jsonb not null default '{}'::jsonb,
    writer_model       text,
    created_at         timestamptz not null default now()
);

create index if not exists deviations_instruction_idx on public.deviations (instruction_source, instruction_ref);
create index if not exists deviations_session_idx on public.deviations (session_id);

-- Same lockdown as the other tables: RLS on with no policies, and no grants for the
-- Supabase API roles. The roles exist on Supabase but not on the plain test Postgres.
alter table public.sessions enable row level security;
alter table public.deviations enable row level security;

do $$
declare r text;
begin
  foreach r in array array['anon', 'authenticated'] loop
    if exists (select 1 from pg_roles where rolname = r) then
      execute format('revoke all on table public.sessions from %I', r);
      execute format('revoke all on table public.deviations from %I', r);
    end if;
  end loop;
end $$;
