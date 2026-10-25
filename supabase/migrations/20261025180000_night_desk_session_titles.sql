-- Night Desk names the session a slip happened in ("Found in: <chat title> · Claude").
-- Existing owner RLS still applies; this grants neither writes nor meta, paths or tokens.
do $$
begin
 if exists (select 1 from pg_roles where rolname = 'authenticated') then
   grant select (title, tool) on public.sessions to authenticated;
 end if;
end $$;
notify pgrst, 'reload schema';
