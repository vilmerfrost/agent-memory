-- What happened to each decision and each edit undo, readable by Night Desk (owner RLS).
-- docs/rsi/activation.md (step 7 and 10b) and docs/rsi/least-privilege.md ("Unattended decide-job").
--
-- rsi_decision_results gets a short fixed reason (reason_code, written by `amem rsi decide-poll`) and the time of
-- the last attempt (updated_at). The owner may also read `detail` (redacted by the worker, at most 500 characters;
-- like edits.target_path it can name a file). The views show only the fixed reason.
-- Two security_invoker views turn the raw rows into one state per decision / undo request:
--   state   waiting | applied | rejected | undone | refused
--   reason  null, or one fixed word (see the check constraint and the CASE below)
--   state_at when the state was reached (handled), last tried (retrying), or recorded (waiting)
-- Nothing here grants a decision path: no insert on rsi_decisions, no function.

alter table public.rsi_decision_results add column if not exists reason_code text;
alter table public.rsi_decision_results add column if not exists updated_at timestamptz not null default now();
alter table public.rsi_decision_results drop constraint if exists rsi_decision_results_reason_code_check;
alter table public.rsi_decision_results add constraint rsi_decision_results_reason_code_check check (
  reason_code is null or reason_code in ('hash-mismatch', 'decision-expired', 'artifact-changed', 'live-file-changed',
                                         'promotion-refused', 'not-promoted', 'undo-conflict', 'retrying', 'gave-up'));

create or replace view public.rsi_decision_status with (security_invoker = true) as
select d.id as decision_id,
       d.proposal_id,
       d.action,
       d.source,
       d.decided_at,
       p.status as proposal_status,
       case when r.handled_at is null then 'waiting'
            when r.result = 'promoted' then 'applied'
            when r.result = 'rejected' then 'rejected'
            when r.result = 'reverted' then 'undone'
            else 'refused' end as state,
       case when r.decision_id is null then null
            when r.handled_at is null then 'retrying'
            when r.result in ('promoted', 'rejected', 'reverted') then null
            else coalesce(r.reason_code, case r.result when 'expired' then 'decision-expired'
                                                       when 'conflict' then 'undo-conflict'
                                                       when 'nothing' then 'not-promoted'
                                                       when 'retry' then 'gave-up'
                                                       else 'promotion-refused' end) end as reason,
       r.change_id,
       coalesce(r.attempts, 0) as attempts,
       coalesce(r.handled_at, r.updated_at, d.decided_at) as state_at
from public.rsi_decisions d
join public.rsi_proposals p on p.id = d.proposal_id
left join public.rsi_decision_results r on r.decision_id = d.id;

-- The older edit loop: undo_requests.result is free text written by `amem edits undo-poll`
-- ('reverted', 'nothing to revert (...)', 'busy, retrying: ...', 'failed (attempt n): ...').
create or replace view public.edit_undo_status with (security_invoker = true) as
select u.id as request_id,
       u.edit_id,
       u.source,
       u.requested_at,
       case when u.handled_at is null then 'waiting'
            when u.result = 'reverted' then 'undone'
            else 'refused' end as state,
       case when u.handled_at is null then case when u.result is null then null else 'retrying' end
            when u.result = 'reverted' then null
            when u.result like 'nothing to revert%' then 'nothing-to-undo'
            when u.result like 'failed%' then 'gave-up'
            else 'undo-failed' end as reason,
       u.attempts,
       coalesce(u.handled_at, u.requested_at) as state_at
from public.undo_requests u;

revoke all on public.rsi_decision_status, public.edit_undo_status from public;
grant select on public.rsi_decision_status, public.edit_undo_status to amem_app, rsi_reader, rsi_writer;

do $$
declare r text;
begin
  -- Supabase grants every new view to the API roles by name (default privileges): take that back.
  foreach r in array array['anon', 'authenticated', 'service_role'] loop
    if exists (select 1 from pg_roles where rolname = r) then
      execute format('revoke all on public.rsi_decision_status, public.edit_undo_status from %I', r);
      execute format('revoke all on public.rsi_decision_results from %I', r);
    end if;
  end loop;
  -- Night Desk (the owner's JWT): the two views and the result rows (owner-only policy, read only).
  if exists (select 1 from pg_roles where rolname = 'authenticated') and to_regprocedure('auth.uid()') is not null then
    grant select (decision_id, result, reason_code, detail, change_id, attempts, handled_at, updated_at)
      on public.rsi_decision_results to authenticated;
    drop policy if exists dashboard_owner_read on public.rsi_decision_results;
    create policy dashboard_owner_read on public.rsi_decision_results for select to authenticated
      using ((select auth.uid()) = (select public.amem_owner_uid()));
    grant select on public.rsi_decision_status, public.edit_undo_status to authenticated;
  end if;
end $$;

notify pgrst, 'reload schema';
