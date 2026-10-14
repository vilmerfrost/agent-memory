-- Skill usage and per-skill outcome scores (docs/skills.md).
--
-- skill_calls: one row per skill invocation from ~/.agents/skill-usage/log.jsonl
-- (`amem ingest skills PATH`).
--   ts          when the skill ran (the log's `t`)
--   skill       skill name
--   invoked_by  the log's `by`: who started it: claude | codex (the agent chose it) | me (Vilmer typed it)
--   agent       which agent ran in that session: claude | codex (free text, not checked)
--   cwd         working directory of the call (null when the log line has none)
--   project     project bucket of the cwd, same rule as sessions.project (never empty)
--   backfill    true when the line was reconstructed later, not logged live
--   source_key  sha256 of the canonical JSON line (idempotent re-ingest)
--   session_id  the sessions row the call ran in, when one matches (same tool, same cwd,
--               ts inside the session's start .. last activity). Null otherwise.
create table if not exists public.skill_calls (
    id          uuid primary key default gen_random_uuid(),
    ts          timestamptz not null,
    skill       text not null,
    invoked_by  text not null check (invoked_by in ('claude', 'codex', 'me')),
    agent       text not null,
    cwd         text,
    project     text not null default 'unknown',
    backfill    boolean not null default false,
    source_key  text not null unique,
    session_id  text references public.sessions (session_id) on delete set null,
    created_at  timestamptz not null default now()
);

create index if not exists skill_calls_skill_ts_idx on public.skill_calls (skill, ts desc);
create index if not exists skill_calls_by_ts_idx on public.skill_calls (invoked_by, ts desc);
create index if not exists skill_calls_unmatched_idx on public.skill_calls (ts) where session_id is null;

-- skill_scores: one row per skill and bucket (`amem skills scores --refresh` rewrites it).
--   bucket      `*` (all calls) | `project:<project>` | `category:<sessions.category>`
--   n_calls     skill calls in the bucket (matched to a session or not)
--   n_sessions  distinct sessions the calls matched
--   n_scored    matched sessions with a final outcome (not active, not null)
--   outcomes    {outcome: sessions} over the matched sessions
--   score       (sum of outcome weights + prior * prior_weight) / (n_scored + prior_weight),
--               0..1; weights and prior in docs/skills.md
create table if not exists public.skill_scores (
    skill         text not null,
    bucket        text not null,
    n_calls       integer not null default 0,
    n_sessions    integer not null default 0,
    n_scored      integer not null default 0,
    outcomes      jsonb not null default '{}'::jsonb,
    score         double precision not null check (score >= 0 and score <= 1),
    refreshed_at  timestamptz not null default now(),
    primary key (skill, bucket)
);

-- Same lockdown as the other tables: RLS on with no policies, and no grants for the
-- Supabase API roles. The roles exist on Supabase but not on the plain test Postgres.
alter table public.skill_calls enable row level security;
alter table public.skill_scores enable row level security;

do $$
declare r text;
begin
  foreach r in array array['anon', 'authenticated'] loop
    if exists (select 1 from pg_roles where rolname = r) then
      execute format('revoke all on table public.skill_calls from %I', r);
      execute format('revoke all on table public.skill_scores from %I', r);
    end if;
  end loop;
end $$;
