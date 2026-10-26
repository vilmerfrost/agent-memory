-- The outcome check (2026-10-26, `amem rsi outcome`, launchd com.vilmerfrost.amem-rsi-outcome at 09:20; see
-- docs/rsi/outcome.md). For every applied change -- a night proposal promoted through Night Desk, or an older
-- edit-loop edit that was kept -- it compares the rule's deviations per session in real sessions before and after
-- the apply time. One row per change, rewritten on every run (the latest verdict); the daily report file keeps the
-- history. It never undoes anything: `suggest_undo` is advice that Night Desk shows next to its Undo button.
--
--   change_kind      proposal (rsi_proposals.id) | edit (edits.id)
--   rule_key         <instruction_source>:<rule_id>, as rsi_candidates.rule_key
--   applied_at       rsi_proposals.promoted_at, or edits.created_at (the row is written just before improve apply)
--   window_days      length of each window (same length before and after; at most the check's window)
--   sessions_*       prod sessions, scanned for deviations, that loaded instruction_source, per window
--   deviations_*     the rule's deviations in those sessions
--   rate_*           deviations per session
--   p_value          one-sided exact test of the after-share (conditional binomial); null when not computed
--   verdict          helped | no-change | worse | too-early
--   suggest_undo     true for worse, and for no-change once the full window has been observed
--   undo_hint        how to undo it (Night Desk Undo, or the terminal command)
create table if not exists public.rsi_outcomes (
    change_kind        text not null check (change_kind in ('proposal', 'edit')),
    change_id          text not null,
    rule_key           text not null,
    instruction_source text not null,
    rule_id            text not null,
    title              text,
    applied_at         timestamptz not null,
    checked_at         timestamptz not null default now(),
    first_checked_at   timestamptz not null default now(),
    window_days        real not null check (window_days >= 0),
    sessions_before    int not null default 0 check (sessions_before >= 0),
    sessions_after     int not null default 0 check (sessions_after >= 0),
    deviations_before  int not null default 0 check (deviations_before >= 0),
    deviations_after   int not null default 0 check (deviations_after >= 0),
    rate_before        real,
    rate_after         real,
    p_value            real check (p_value is null or (p_value >= 0 and p_value <= 1)),
    verdict            text not null check (verdict in ('helped', 'no-change', 'worse', 'too-early')),
    reason             text not null check (length(reason) <= 500),
    suggest_undo       boolean not null default false,
    undo_hint          text check (undo_hint is null or length(undo_hint) <= 300),
    meta               jsonb not null default '{}'::jsonb,
    primary key (change_kind, change_id)
);
create index if not exists rsi_outcomes_verdict_idx on public.rsi_outcomes (verdict, checked_at desc);

-- RLS on. The routine role writes it; the RSI read roles read it; Night Desk's owner reads it; no API role else.
alter table public.rsi_outcomes enable row level security;

do $$
declare r text;
begin
  foreach r in array array['anon', 'authenticated', 'service_role'] loop
    if exists (select 1 from pg_roles where rolname = r) then
      execute format('revoke all on table public.rsi_outcomes from %I', r);
    end if;
  end loop;
  if exists (select 1 from pg_roles where rolname = 'amem_app') then
    grant select, insert, update on public.rsi_outcomes to amem_app;
    drop policy if exists amem_app_all on public.rsi_outcomes;
    create policy amem_app_all on public.rsi_outcomes for all to amem_app using (true) with check (true);
  end if;
  if exists (select 1 from pg_roles where rolname = 'rsi_reader') then
    grant select on public.rsi_outcomes to rsi_reader, rsi_writer;
    drop policy if exists rsi_read on public.rsi_outcomes;
    create policy rsi_read on public.rsi_outcomes for select to rsi_reader, rsi_writer using (true);
  end if;
  if exists (select 1 from pg_roles where rolname = 'authenticated') and to_regprocedure('auth.uid()') is not null
     and to_regprocedure('public.amem_owner_uid()') is not null then
    grant select on public.rsi_outcomes to authenticated;
    drop policy if exists dashboard_owner_read on public.rsi_outcomes;
    create policy dashboard_owner_read on public.rsi_outcomes for select to authenticated
      using ((select auth.uid()) = (select public.amem_owner_uid()));
  end if;
end $$;

notify pgrst, 'reload schema';
