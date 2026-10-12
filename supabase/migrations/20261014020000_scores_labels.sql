-- Jev scores and human labels per run (roadmap B2-B6, docs/scoring.md).
--
-- scores: one row per (run, question). Written by `amem score`.
--   value      the answer as text: the label (choice), the expected score (score), P(yes) (noul)
--   value_num  the numeric answer: null for choice, the expected score, or P(yes)
--   confidence from Jev for choice and score; null for noul (Jev gives none)
-- labels: Vilmer's verdict on a run. One current label per run; a relabel overwrites it.
create table if not exists public.scores (
    id            uuid primary key default gen_random_uuid(),
    run_id        uuid not null references public.runs (id) on delete cascade,
    question      text not null,
    kind          text not null check (kind in ('choice', 'score', 'noul')),
    value         text not null,
    value_num     double precision,
    confidence    double precision check (confidence is null or (confidence >= 0 and confidence <= 1)),
    probabilities jsonb not null default '{}'::jsonb,
    jev_mode      text not null check (jev_mode in ('fixture', 'live')),
    created_at    timestamptz not null default now(),
    constraint scores_run_id_question_key unique (run_id, question)
);

create table if not exists public.labels (
    id         uuid primary key default gen_random_uuid(),
    run_id     uuid not null references public.runs (id) on delete cascade,
    verdict    text not null check (verdict in ('up', 'down')),
    note       text,
    labeled_at timestamptz not null default now(),
    constraint labels_run_id_key unique (run_id)
);

-- Same lockdown as 20261012230000_rls_lockdown.sql: RLS on with no policies, so the
-- Supabase Data API roles get nothing. amem connects as the owner and is unaffected.
alter table public.scores enable row level security;
alter table public.labels enable row level security;

-- The roles exist on Supabase but not on the plain test Postgres.
do $$
declare r text;
begin
  foreach r in array array['anon', 'authenticated'] loop
    if exists (select 1 from pg_roles where rolname = r) then
      execute format('revoke all on table public.scores from %I', r);
      execute format('revoke all on table public.labels from %I', r);
    end if;
  end loop;
end $$;
