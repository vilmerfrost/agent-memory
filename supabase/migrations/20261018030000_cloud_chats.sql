-- Cloud chats (docs/cloud.md, "Cloud chats"). Interactive claude.ai/code sessions send their
-- digest from a Stop / SessionEnd hook (`amem_digest.py --hook`), not only routines from their
-- last step. A hook row is kind `chat` and has no routine name:
--   POST /rest/v1/cloud_digests?on_conflict=source_key
--   Prefer: resolution=merge-duplicates,return=minimal      body {..., kind: chat, ingested_at: null}
-- so every hook POST refreshes the row and `sessions ingest` reads it again.
--
-- One cloud session can be both: a routine's own last step (kind routine, ignore-duplicates)
-- and the hook. The routine's step is authoritative for kind, routine name and instructions,
-- in either order:
--   * hook after routine: the BEFORE UPDATE trigger keeps kind, routine and instructions; the
--     digest, times, skills and meta (tokens) come from the hook (it read more of the session).
--   * routine after hook: the routine's POST is ignore-duplicates (ON CONFLICT DO NOTHING), so
--     the BEFORE INSERT trigger folds the chat row into the routine row: the chat row is
--     deleted and the routine row is inserted with the newer digest of the two.
alter table public.cloud_digests add column if not exists kind text not null default 'routine';
alter table public.cloud_digests alter column routine drop not null;
alter table public.cloud_digests drop constraint if exists cloud_digests_kind_check;
alter table public.cloud_digests add constraint cloud_digests_kind_check
    check (kind in ('routine', 'chat') and (kind = 'routine') = (routine is not null));

create or replace function public.cloud_digests_keep_routine() returns trigger
language plpgsql
set search_path = public, pg_temp
as $$
begin
  if old.kind = 'routine' and new.kind = 'chat' then
    new.kind := 'routine';
    new.routine := old.routine;
    new.instructions := old.instructions;
  end if;
  return new;
end
$$;

create or replace function public.cloud_digests_claim_chat() returns trigger
language plpgsql
set search_path = public, pg_temp
as $$
declare
  old_row public.cloud_digests%rowtype;
begin
  if new.kind <> 'routine' then
    return new;
  end if;
  delete from public.cloud_digests
  where source_key = new.source_key and kind = 'chat'
  returning * into old_row;
  if not found then
    return new;
  end if;
  new.started_at := least(new.started_at, old_row.started_at);
  new.created_at := old_row.created_at;
  if old_row.ended_at is not null and (new.ended_at is null or old_row.ended_at > new.ended_at) then
    new.ended_at := old_row.ended_at;
    new.digest := old_row.digest;
    new.digest_sha := old_row.digest_sha;
    new.skills := old_row.skills;
    new.meta := old_row.meta;
  end if;
  new.ingested_at := null;
  return new;
end
$$;

drop trigger if exists cloud_digests_keep_routine on public.cloud_digests;
create trigger cloud_digests_keep_routine before update on public.cloud_digests
    for each row execute function public.cloud_digests_keep_routine();

drop trigger if exists cloud_digests_claim_chat on public.cloud_digests;
create trigger cloud_digests_claim_chat before insert on public.cloud_digests
    for each row execute function public.cloud_digests_claim_chat();

-- The trigger functions are not part of the API.
do $$
declare r text;
begin
  foreach r in array array['anon', 'authenticated'] loop
    if exists (select 1 from pg_roles where rolname = r) then
      execute format('revoke all on function public.cloud_digests_keep_routine() from %I', r);
      execute format('revoke all on function public.cloud_digests_claim_chat() from %I', r);
    end if;
  end loop;
end $$;
revoke all on function public.cloud_digests_keep_routine() from public;
revoke all on function public.cloud_digests_claim_chat() from public;
