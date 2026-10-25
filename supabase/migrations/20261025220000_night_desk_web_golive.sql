-- GO-LIVE STEP for web Apply / Reject / Undo (docs/rsi/approval.md "Go-live").
-- NOT applied by `amem db migrate`: it lives outside supabase/migrations on purpose.
-- Apply it together with the Night Desk deploy that uses amem-decide /web/*, not before: the older Night Desk
-- still calls amem_request_undo_owner for its Undo button.
--
--   git mv supabase/migrations-golive/20261025220000_night_desk_web_golive.sql supabase/migrations/
--   uv run amem db migrate
--
-- Why: amem_request_undo_owner lets any signed-in owner session undo an edit with no Face ID. After this step,
-- an edit undo from the browser goes only through amem-decide /web/decide (passkey assertion required).
-- Undo stays available in the terminal (`amem edits undo`) and through amem_request_undo for service_role.

do $$
begin
  if exists (select 1 from pg_roles where rolname = 'authenticated') then
    revoke all on function public.amem_request_undo_owner(uuid) from authenticated;
  end if;
end $$;
revoke all on function public.amem_request_undo_owner(uuid) from public;

notify pgrst, 'reload schema';
