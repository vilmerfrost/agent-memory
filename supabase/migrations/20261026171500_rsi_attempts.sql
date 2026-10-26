-- Experiment-attempt identity (plan Revision 2, R1; docs/data-use/relationships.md, `experiment_attempt_sessions`).
--
-- rsi_attempts: one row per sandbox experiment attempt (arm A or B of one case and repetition), or per
-- attempt-like record that carries no explicit identity (status unresolved, with a reason).
--
--   id                  linked: the canonical attempt key `<experiment_id>/<arm>/<case_key>/<rep>` (integrations.refs
--                       run_key; case_key = `<profile>.<case>` for a provider-profile replay, else the case id)
--                       unresolved: `unresolved:<what>:<locator>`
--   attempt_session_id  uuid5 of the key (refs.session_uuid): the id the runner, the run record, the sidecar and the
--                       Langfuse trace (amem_session_id) all carry
--   session_id          an ingested sessions row tied to this attempt by an exact id match (never by time)
--   source              runner: written when the attempt ran (rsi_night experiment stage)
--                       repair: `amem coverage repair-links --apply` from explicit identifiers (undo manifest)
--   evidence            the explicit identifiers behind the row (paths relative to the RSI sandbox root, refs)
--
-- Not sessions rows on purpose: ingest rewrites sessions.env / experiment_id on every pass and sessions feed the
-- transcript consumers (deviation scan, summaries), which an attempt record is not.
--
-- Access: RLS on. amem_app reads, inserts and updates; it deletes only repair rows (undo of a repair). Night
-- Desk's owner reads. No other role (anon, authenticated non-owner, service_role, rsi_reader) gets anything.

create table if not exists public.rsi_attempts (
    id                 text primary key check (length(id) between 1 and 300),
    status             text not null check (status in ('linked', 'unresolved')),
    experiment_id      text check (experiment_id is null or length(experiment_id) <= 80),
    profile            text check (profile is null or profile ~ '^[a-z0-9][a-z0-9_-]{0,31}$'),
    arm                text check (arm is null or arm ~ '^[A-Z]$'),
    case_id            text check (case_id is null or length(case_id) <= 120),
    rep                integer check (rep is null or rep >= 0),
    attempt_session_id text unique,
    run_id             text check (run_id is null or length(run_id) <= 120),
    session_id         text check (session_id is null or length(session_id) <= 200),
    source             text not null check (source in ('runner', 'repair')),
    evidence           jsonb not null default '{}'::jsonb
                       check (jsonb_typeof(evidence) = 'object' and octet_length(evidence::text) <= 4000),
    reason             text check (reason is null or length(reason) between 1 and 500),
    created_at         timestamptz not null default now(),
    updated_at         timestamptz not null default now(),
    constraint rsi_attempts_status_check2 check (coalesce(case status
        when 'linked' then experiment_id is not null and arm is not null and case_id is not null and rep is not null
                           and attempt_session_id is not null and reason is null
        when 'unresolved' then reason is not null and attempt_session_id is null
        end, false))
);
create index if not exists rsi_attempts_experiment_idx on public.rsi_attempts (experiment_id) where experiment_id is not null;
create index if not exists rsi_attempts_run_idx on public.rsi_attempts (run_id) where run_id is not null;
create index if not exists rsi_attempts_session_idx on public.rsi_attempts (session_id) where session_id is not null;

alter table public.rsi_attempts enable row level security;
revoke all on public.rsi_attempts from public;

do $$
declare r text;
begin
  foreach r in array array['anon', 'authenticated', 'service_role', 'rsi_reader', 'rsi_writer'] loop
    if exists (select 1 from pg_roles where rolname = r) then
      execute format('revoke all on public.rsi_attempts from %I', r);
    end if;
  end loop;
  if exists (select 1 from pg_roles where rolname = 'amem_app') then
    grant select, insert, update, delete on public.rsi_attempts to amem_app;
    drop policy if exists rsi_attempts_app_read on public.rsi_attempts;
    create policy rsi_attempts_app_read on public.rsi_attempts for select to amem_app using (true);
    drop policy if exists rsi_attempts_app_insert on public.rsi_attempts;
    create policy rsi_attempts_app_insert on public.rsi_attempts for insert to amem_app with check (true);
    drop policy if exists rsi_attempts_app_update on public.rsi_attempts;
    create policy rsi_attempts_app_update on public.rsi_attempts for update to amem_app using (true) with check (true);
    drop policy if exists rsi_attempts_app_undo on public.rsi_attempts;
    create policy rsi_attempts_app_undo on public.rsi_attempts for delete to amem_app using (source = 'repair');
  end if;
  -- Night Desk: the owner's JWT reads (no writes); any other signed-in user sees no rows.
  if exists (select 1 from pg_roles where rolname = 'authenticated') and to_regprocedure('auth.uid()') is not null
     and to_regprocedure('public.amem_owner_uid()') is not null then
    grant select on public.rsi_attempts to authenticated;
    drop policy if exists dashboard_owner_read on public.rsi_attempts;
    create policy dashboard_owner_read on public.rsi_attempts for select to authenticated
      using ((select auth.uid()) = (select public.amem_owner_uid()));
  end if;
end $$;

notify pgrst, 'reload schema';
