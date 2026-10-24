-- Night Desk: owner-only reads, with one narrow undo operation.
create schema if not exists amem_private;
revoke all on schema amem_private from public;
create table if not exists amem_private.dashboard_owner (
 singleton boolean primary key default true check (singleton), uid uuid not null unique
);
revoke all on amem_private.dashboard_owner from public;

create or replace function public.amem_owner_uid() returns uuid
language sql stable security definer set search_path = ''
as $$ select uid from amem_private.dashboard_owner where singleton $$;
revoke all on function public.amem_owner_uid() from public;

create table if not exists public.rsi_nights (
 night text primary key, status text not null, headline text not null,
 brief jsonb not null, report jsonb not null, costs jsonb not null, checkpoint jsonb not null,
 created_at timestamptz not null default now(), updated_at timestamptz not null default now()
);
create table if not exists public.report_snapshots (
 kind text not null check (kind in ('evals-parent','skill-evals','improve','secret-scan','config-check','routine-audit')),
 date date not null, summary jsonb not null, cost_usd numeric check (cost_usd >= 0), report_path text not null,
 created_at timestamptz not null default now(), updated_at timestamptz not null default now(), primary key(kind,date)
);
create table if not exists public.ledger_events (
 id text primary key, event text not null, occurred_at timestamptz, summary jsonb not null,
 created_at timestamptz not null default now()
);
alter table public.rsi_nights enable row level security;
alter table public.report_snapshots enable row level security;
alter table public.ledger_events enable row level security;
create index if not exists ledger_events_time_idx on public.ledger_events (occurred_at desc, id);

create or replace view public.dashboard_sessions with (security_invoker = true) as
 select session_id, kind, repo, started_at, ended_at, scanned_at, created_at from public.sessions;
grant select on public.dashboard_sessions to amem_app;

create or replace function public.amem_request_undo_owner(edit_id uuid) returns text
language plpgsql security definer set search_path = '' as $$
declare outcome text;
begin
 if auth.uid() is null or auth.uid() is distinct from public.amem_owner_uid() then
   raise exception 'Not allowed' using errcode = '42501';
 end if;
 outcome := public.amem_request_undo(edit_id, 'phone');
 return case outcome when 'queued' then 'queued' when 'reverted' then 'already undone' else 'nothing to undo' end;
end $$;
revoke all on function public.amem_request_undo_owner(uuid) from public;

do $$
declare t text; r text;
begin
 foreach r in array array['anon','authenticated','service_role'] loop
   if exists (select 1 from pg_roles where rolname=r) then
     execute format('revoke all on public.rsi_nights,public.report_snapshots,public.ledger_events,public.dashboard_sessions from %I', r);
     execute format('revoke all on function public.amem_owner_uid(),public.amem_request_undo_owner(uuid) from %I', r);
   end if;
 end loop;
 foreach t in array array['rsi_nights','report_snapshots','ledger_events'] loop
   execute format('grant select, insert, update on public.%I to amem_app', t);
   execute format('drop policy if exists dashboard_ingest on public.%I', t);
   execute format('create policy dashboard_ingest on public.%I for all to amem_app using (true) with check (true)', t);
 end loop;
 if exists (select 1 from pg_roles where rolname='authenticated') and to_regprocedure('auth.uid()') is not null then
   revoke all on public.rsi_nights,public.report_snapshots,public.ledger_events from authenticated;
   grant execute on function public.amem_owner_uid() to authenticated;
   grant execute on function public.amem_request_undo_owner(uuid) to authenticated;
   foreach t in array array['rsi_nights','report_snapshots','ledger_events','runs','edits','undo_requests',
     'rsi_candidates','rsi_experiments','rsi_proposals','rsi_checkpoints','deviations','sessions','rsi_decisions'] loop
     execute format('drop policy if exists dashboard_owner_read on public.%I', t);
     execute format('create policy dashboard_owner_read on public.%I for select to authenticated using ((select auth.uid()) = (select public.amem_owner_uid()))', t);
     if t not in ('sessions','edits') then execute format('grant select on public.%I to authenticated', t); end if;
   end loop;
   -- No transcript paths, summaries, raw metadata, or undo-token hashes reach the browser.
   revoke all on public.sessions, public.edits from authenticated;
   grant select (session_id,kind,repo,started_at,ended_at,scanned_at,created_at) on public.sessions to authenticated;
   grant select on public.dashboard_sessions to authenticated;
   grant select (id,improve_id,status,target_path,instruction_source,rule_id,rule_title,why,summary,
      created_at,reverted_at,undo_requested_at) on public.edits to authenticated;
 end if;
 if exists (select 1 from pg_roles where rolname='anon') then
   revoke all on public.rsi_nights,public.report_snapshots,public.ledger_events,public.dashboard_sessions from anon;
   revoke all on function public.amem_owner_uid(),public.amem_request_undo_owner(uuid) from anon;
 end if;
end $$;
notify pgrst, 'reload schema';
