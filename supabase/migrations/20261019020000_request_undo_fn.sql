-- amem_request_undo: queue (or reopen) the undo request for one edit. One code path for the
-- phone (edge function amem-undo, via PostgREST rpc) and the CLI (`amem edits undo`).
--
-- A request that was closed without a revert (failed 3 times, or "nothing to revert") is
-- reopened: handled_at, attempts and result reset, so `amem edits undo-poll` tries again.
-- A request that already reverted the edit is left alone.
--
-- Returns what will happen, so the caller never has to guess:
--   missing   no such edit (nothing written)
--   reverted  the edit was already undone (nothing written)
--   nothing   queued, but the edit is not kept/trial, so the poll will find nothing to revert
--   queued    queued or reopened; the poll reverts it
create or replace function public.amem_request_undo(p_edit_id uuid, p_source text) returns text
language plpgsql
set search_path = public, pg_temp
as $$
declare
  e record;
begin
  select status, improve_id into e from public.edits where id = p_edit_id;
  if not found then
    return 'missing';
  end if;
  insert into public.undo_requests as u (edit_id, source) values (p_edit_id, p_source)
  on conflict (edit_id) do update
    set handled_at = null, attempts = 0, source = excluded.source, result = null, requested_at = now()
    where u.result is distinct from 'reverted';
  if not found then
    return 'reverted';
  end if;
  if e.status not in ('kept', 'trial') or e.improve_id is null then
    return 'nothing';
  end if;
  return 'queued';
end
$$;

-- Not part of the public API: only the service role (edge function) and the owner (CLI) call it.
revoke all on function public.amem_request_undo(uuid, text) from public;
do $$
declare r text;
begin
  foreach r in array array['anon', 'authenticated'] loop
    if exists (select 1 from pg_roles where rolname = r) then
      execute format('revoke all on function public.amem_request_undo(uuid, text) from %I', r);
    end if;
  end loop;
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    grant execute on function public.amem_request_undo(uuid, text) to service_role;
  end if;
end $$;

notify pgrst, 'reload schema';
