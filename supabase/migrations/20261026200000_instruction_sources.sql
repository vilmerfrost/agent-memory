-- Instruction sources with authority and version (plan section 5; docs/data-use/instruction-sources.md).
--
-- sessions.instruction_sources: one JSON entry per instruction file in force for the session:
--   {name, kind, path, authority, reason?, version?, sha256?, plugin?, imported_by?, changed_during_session?, stored?}
--   authority: observed | reconstructed | declared | unresolved. Null: not captured yet (a historical row;
--   `amem sessions instruction-sources --apply` fills it). sessions.instructions keeps the source names.
-- deviations.instruction_authority: how sure the text a deviation was graded against is
--   (observed | reconstructed | unverified = today's file). Null: scanned before this column existed.
-- instruction_texts: the instruction texts of observed and reconstructed versions, content-addressed by the
--   sha256 of the raw text, stored redacted (the shared redaction), at most 100 kB, only from the allowed roots,
--   never env files. Insert-only: a version's text never changes.
--
-- Access: RLS on. amem_app (ingest, scan, reconcile) reads and inserts texts; Night Desk's owner reads; no
-- other role (anon, authenticated non-owner, service_role, rsi_reader, rsi_writer) gets anything. Night Desk's
-- column grants on sessions / deviations do not include the new columns.

alter table public.sessions add column if not exists instruction_sources jsonb;
alter table public.sessions drop constraint if exists sessions_instruction_sources_check;
alter table public.sessions add constraint sessions_instruction_sources_check check (
    instruction_sources is null
    or (jsonb_typeof(instruction_sources) = 'array' and octet_length(instruction_sources::text) <= 100000));

alter table public.deviations add column if not exists instruction_authority text;
alter table public.deviations drop constraint if exists deviations_instruction_authority_check;
alter table public.deviations add constraint deviations_instruction_authority_check check (
    instruction_authority is null or instruction_authority in ('observed', 'reconstructed', 'unverified'));

create table if not exists public.instruction_texts (
    sha256     text primary key check (sha256 ~ '^[0-9a-f]{64}$'),
    text       text not null check (octet_length(text) <= 200000),
    bytes      integer not null check (bytes between 0 and 100000),
    redacted   boolean not null default false,
    path       text not null check (length(path) between 1 and 500),
    created_at timestamptz not null default now()
);

alter table public.instruction_texts enable row level security;
revoke all on public.instruction_texts from public;

do $$
declare r text;
begin
  foreach r in array array['anon', 'authenticated', 'service_role', 'rsi_reader', 'rsi_writer'] loop
    if exists (select 1 from pg_roles where rolname = r) then
      execute format('revoke all on public.instruction_texts from %I', r);
    end if;
  end loop;
  if exists (select 1 from pg_roles where rolname = 'amem_app') then
    grant select, insert on public.instruction_texts to amem_app;
    drop policy if exists instruction_texts_app_read on public.instruction_texts;
    create policy instruction_texts_app_read on public.instruction_texts for select to amem_app using (true);
    drop policy if exists instruction_texts_app_insert on public.instruction_texts;
    create policy instruction_texts_app_insert on public.instruction_texts for insert to amem_app with check (true);
  end if;
  -- Night Desk: the owner's JWT reads (no writes); any other signed-in user sees no rows.
  if exists (select 1 from pg_roles where rolname = 'authenticated') and to_regprocedure('auth.uid()') is not null
     and to_regprocedure('public.amem_owner_uid()') is not null then
    grant select on public.instruction_texts to authenticated;
    drop policy if exists dashboard_owner_read on public.instruction_texts;
    create policy dashboard_owner_read on public.instruction_texts for select to authenticated
      using ((select auth.uid()) = (select public.amem_owner_uid()));
  end if;
end $$;

notify pgrst, 'reload schema';
