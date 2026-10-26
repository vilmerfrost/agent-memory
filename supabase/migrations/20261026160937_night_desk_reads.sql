-- Read receipts belong to the single configured Night Desk owner. Reports and
-- their approval/failure state stay untouched; absence of a receipt means unread.
create table public.night_reads (
 night text primary key references public.rsi_nights(night) on delete cascade,
 read_at timestamptz not null default now()
);
alter table public.night_reads enable row level security;
revoke all on public.night_reads from public;

-- One statement covers the entire backlog, even beyond the API's row limit.
-- Explicit IDs support a single night or an expanded group. New nights that
-- arrive after this statement remain unread. Invoker rights retain report RLS.
create function public.amem_mark_nights_read(requested_nights text[] default null)
returns integer language sql security invoker set search_path = '' as $$
 with marked as (
   insert into public.night_reads(night)
   select n.night from public.rsi_nights n
   where requested_nights is null or n.night = any(requested_nights)
   on conflict (night) do nothing
   returning 1
 ) select count(*)::integer from marked
$$;
revoke all on function public.amem_mark_nights_read(text[]) from public;

do $$
declare r text;
begin
 foreach r in array array['anon','authenticated','service_role','amem_app'] loop
   if exists (select 1 from pg_roles where rolname=r) then
     execute format('revoke all on public.night_reads from %I', r);
     execute format('revoke all on function public.amem_mark_nights_read(text[]) from %I', r);
   end if;
 end loop;
 if exists (select 1 from pg_roles where rolname='authenticated')
    and to_regprocedure('auth.uid()') is not null then
   grant select, insert, delete on public.night_reads to authenticated;
   create policy night_reads_owner_select on public.night_reads for select to authenticated
     using ((select auth.uid()) = (select public.amem_owner_uid()));
   create policy night_reads_owner_insert on public.night_reads for insert to authenticated
     with check ((select auth.uid()) = (select public.amem_owner_uid()));
   create policy night_reads_owner_delete on public.night_reads for delete to authenticated
     using ((select auth.uid()) = (select public.amem_owner_uid()));
   grant execute on function public.amem_mark_nights_read(text[]) to authenticated;
 end if;
end $$;
notify pgrst, 'reload schema';
