-- Run log: one row per routine run. Plain Postgres, no RLS yet (local only).
create table if not exists public.runs (
    id             uuid primary key default gen_random_uuid(),
    routine        text not null,
    source         text check (source is null or source in ('launchd', 'scheduled-task', 'cloud', 'manual')),
    started_at     timestamptz default now(),
    finished_at    timestamptz,
    status         text not null check (status in ('pass', 'fail', 'skip', 'error')),
    summary        text,
    failure_reason text,
    report_path    text,
    meta           jsonb not null default '{}'::jsonb,
    created_at     timestamptz not null default now()
);

create index if not exists runs_routine_started_at_idx
    on public.runs (routine, started_at desc);
