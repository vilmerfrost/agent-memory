-- Readable evidence and exact history for Night Desk. Existing owner RLS still
-- applies; this grants neither writes nor private edit metadata/undo tokens.
do $$
begin
 if exists (select 1 from pg_roles where rolname = 'authenticated') then
   grant select (deviation_ids, instruction_sha_before, instruction_sha_after)
     on public.edits to authenticated;
 end if;
end $$;
notify pgrst, 'reload schema';
