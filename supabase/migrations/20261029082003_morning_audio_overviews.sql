-- Independent audio queue. Worker claims never reach the owner API.
create table amem_private.morning_overviews (
 id uuid primary key default gen_random_uuid(), night date not null,
 manual boolean not null default false, requested_at timestamptz not null default now(),
 state text not null default 'queued' check (state in ('queued','scripting','text_ready','narrating','uploading','ready','failed','outcome_unknown')),
 claim_id uuid, claimed_at timestamptz, updated_at timestamptz not null default now(),
 data jsonb not null default '{}'::jsonb,
 object_path text generated always as ('overviews/' || id::text || '.mp3') stored
);
create index morning_overviews_night on amem_private.morning_overviews(night, requested_at desc);
alter table amem_private.morning_overviews enable row level security;
revoke all on amem_private.morning_overviews from public;

create table public.report_annotations (
 experiment_id text primary key, revision integer not null default 1,
 explanation text not null, source text not null, correction_date date not null, recorded_at timestamptz not null default now()
);
alter table public.report_annotations enable row level security;
revoke all on public.report_annotations from public;
insert into public.report_annotations(experiment_id,explanation,source,correction_date)
 select x, 'Later checker defects make the recorded improvement or regression conclusion unreliable. Nothing was applied from this experiment.',
 'https://github.com/vilmerfrost/agent-memory/blob/f3075a917bd21ad4d3dc13b9cb1f7e5a27ac891b/docs/rsi/evidence-integrity-2026-10-28.md', '2026-10-28'::date
 from unnest(array['exp-20261027-5a8bda33','exp-20261027-7c731284','exp-20261027-90b819e4']) x;

create function public.amem_audio_enqueue(p_night date,p_manual boolean default false) returns jsonb
language plpgsql security definer set search_path='' as $$
declare r amem_private.morning_overviews; n integer;
begin
 if p_night <> (now() at time zone 'Europe/Stockholm')::date-1 then
   raise exception 'night out of range'; end if;
 perform pg_advisory_xact_lock(hashtext('morning-audio:'||p_night::text));
 select * into r from amem_private.morning_overviews where night=p_night order by requested_at desc limit 1;
 if r.id is not null and r.state in ('queued','scripting','text_ready','narrating','uploading') then
   return jsonb_build_object('accepted',false,'id',r.id,'reason','already_running'); end if;
 if r.id is not null and not p_manual then return jsonb_build_object('accepted',false,'id',r.id,'reason','already_requested'); end if;
 select count(*) into n from amem_private.morning_overviews where night=p_night;
 if n>=2 then return jsonb_build_object('accepted',false,'reason','daily_limit'); end if;
 if p_manual and exists(select 1 from amem_private.morning_overviews where night=p_night and manual) then
   return jsonb_build_object('accepted',false,'reason','daily_limit'); end if;
 if r.id is not null and r.requested_at>now()-interval '5 minutes' then
   return jsonb_build_object('accepted',false,'reason','cooldown'); end if;
 insert into amem_private.morning_overviews(night,manual) values(p_night,p_manual) returning * into r;
 return jsonb_build_object('accepted',true,'id',r.id);
end $$;
revoke all on function public.amem_audio_enqueue(date,boolean) from public;

create function public.amem_audio_request(p_night date) returns jsonb
language plpgsql security definer set search_path='' as $$
begin
 if auth.uid() is null or auth.uid() is distinct from public.amem_owner_uid() then raise exception 'Not allowed' using errcode='42501'; end if;
 return public.amem_audio_enqueue(p_night,true);
end $$;
revoke all on function public.amem_audio_request(date) from public;

create function public.amem_audio_claim() returns jsonb
language plpgsql security definer set search_path='' as $$
declare r amem_private.morning_overviews;
begin
 update amem_private.morning_overviews set state='outcome_unknown',updated_at=now(),
 data=data||'{"error":"worker_interrupted"}'::jsonb
 where state in ('scripting','text_ready','narrating','uploading') and claimed_at<now()-interval '20 minutes';
 select * into r from amem_private.morning_overviews where state='queued' order by requested_at for update skip locked limit 1;
 if not found then return null; end if;
 update amem_private.morning_overviews set state='scripting',claim_id=gen_random_uuid(),claimed_at=now(),updated_at=now()
 where id=r.id returning * into r;
 return jsonb_build_object('id',r.id,'night',r.night,'claim_id',r.claim_id,'object_path',r.object_path);
end $$;
revoke all on function public.amem_audio_claim() from public;

create function public.amem_audio_save(p_id uuid,p_claim uuid,p_state text,p_data jsonb) returns boolean
language plpgsql security definer set search_path='' as $$
declare r amem_private.morning_overviews;
begin
 select * into r from amem_private.morning_overviews where id=p_id and claim_id=p_claim for update;
 if not found or r.claimed_at<now()-interval '20 minutes' then return false; end if;
 if r.state in ('uploading','ready') and p_state in ('uploading','ready') and p_data->>'checksum'=r.data->>'checksum' and p_data->>'bytes'=r.data->>'bytes' then
   if r.state='ready' or p_state='uploading' then return true; end if;
 end if;
 if r.state in ('ready','failed','outcome_unknown') then return false; end if;
 if not (p_state in ('failed','outcome_unknown') or
 (r.state='scripting' and p_state='text_ready') or (r.state='text_ready' and p_state='narrating') or
 (r.state='narrating' and p_state='uploading') or (r.state='uploading' and p_state='ready')) then return false; end if;
 if jsonb_typeof(p_data)<>'object' or length(coalesce(p_data->>'transcript',''))>9000 then return false; end if;
 if p_state='ready' and (coalesce((p_data->>'bytes')::integer,0)<100 or coalesce(p_data->>'checksum','') !~ '^[0-9a-f]{64}$') then return false; end if;
 update amem_private.morning_overviews set state=p_state,data=data||p_data,updated_at=now() where id=p_id;
 return true;
end $$;
revoke all on function public.amem_audio_save(uuid,uuid,text,jsonb) from public;

-- Narrow server broker lookup: a live unpredictable claim is the upload capability.
create function public.amem_audio_upload_context(p_id uuid,p_claim uuid) returns jsonb
language sql stable security definer set search_path='' as $$
 select jsonb_build_object('path',object_path) from amem_private.morning_overviews
 where id=p_id and claim_id=p_claim and state in ('uploading','ready') and claimed_at>now()-interval '20 minutes'
$$;
revoke all on function public.amem_audio_upload_context(uuid,uuid) from public;

create function public.amem_audio_status(p_night date) returns jsonb
language plpgsql stable security definer set search_path='' as $$
declare r amem_private.morning_overviews; previous jsonb;
begin
 if auth.uid() is null or auth.uid() is distinct from public.amem_owner_uid() then raise exception 'Not allowed' using errcode='42501'; end if;
 select * into r from amem_private.morning_overviews where night=p_night order by requested_at desc limit 1;
 if not found then return jsonb_build_object('state','not_requested','night',p_night); end if;
 select jsonb_build_object('id',id,'data',data,'state','ready','requested_at',requested_at) into previous
 from amem_private.morning_overviews where night=p_night and state='ready' and id<>r.id order by requested_at desc limit 1;
 return jsonb_build_object('id',r.id,'night',r.night,'state',case when r.state not in ('queued','ready','failed','outcome_unknown') and r.claimed_at<now()-interval '20 minutes' then 'outcome_unknown' else r.state end,
 'requested_at',r.requested_at,'updated_at',r.updated_at,'data',r.data,'previous',previous,
 'outdated_ids',(select coalesce(jsonb_agg(o.id),'[]'::jsonb) from amem_private.morning_overviews o where o.night=p_night and o.state='ready'
 and exists(select 1 from public.report_annotations a where o.data->'snapshot'->'annotations'->>a.experiment_id is distinct from a.explanation
 and exists(select 1 from jsonb_array_elements(o.data->'snapshot'->'facts') f where f->>'id'='experiment:'||a.experiment_id))),
 'newer_results',exists(select 1 from public.runs where started_at > nullif(r.data->>'as_of','')::timestamptz
 and started_at < ((p_night+1)::text||' 12:00 Europe/Stockholm')::timestamptz),
 'outdated',exists(select 1 from public.report_annotations a where r.data->'snapshot'->'annotations'->>a.experiment_id is distinct from a.explanation
 and exists(select 1 from jsonb_array_elements(r.data->'snapshot'->'facts') f where f->>'id'='experiment:'||a.experiment_id)));
end $$;
revoke all on function public.amem_audio_status(date) from public;

create function public.amem_audio_playback(p_id uuid) returns jsonb
language plpgsql stable security definer set search_path='' as $$
declare r amem_private.morning_overviews;
begin
 if auth.uid() is null or auth.uid() is distinct from public.amem_owner_uid() then raise exception 'Not allowed' using errcode='42501'; end if;
 select * into r from amem_private.morning_overviews where id=p_id and state='ready';
 if not found then return null; end if;
 if exists(select 1 from public.report_annotations a where r.data->'snapshot'->'annotations'->>a.experiment_id is distinct from a.explanation
 and exists(select 1 from jsonb_array_elements(r.data->'snapshot'->'facts') f where f->>'id'='experiment:'||a.experiment_id)) then
   return jsonb_build_object('outdated',true); end if;
 return jsonb_build_object('path',r.object_path);
end $$;
revoke all on function public.amem_audio_playback(uuid) from public;

do $$ declare r text; begin
 grant execute on function public.amem_audio_enqueue(date,boolean), public.amem_audio_claim(),public.amem_audio_save(uuid,uuid,text,jsonb) to amem_app;
 grant select on public.report_annotations to amem_app;
 create policy audio_annotations_app on public.report_annotations for select to amem_app using(true);
 foreach r in array array['authenticated','service_role'] loop
 if exists(select 1 from pg_roles where rolname=r) then
   execute format('revoke all on public.report_annotations from %I',r);
   execute format('grant execute on function public.amem_audio_request(date),public.amem_audio_status(date),public.amem_audio_playback(uuid) to %I',r);
 end if; end loop;
 if exists(select 1 from pg_roles where rolname='anon') then revoke all on public.report_annotations from anon; end if;
 if exists(select 1 from pg_roles where rolname='service_role') then
 grant execute on function public.amem_audio_upload_context(uuid,uuid) to service_role; end if;
 if exists(select 1 from pg_roles where rolname='authenticated') then
 grant select on public.report_annotations to authenticated;
 create policy audio_annotations_owner on public.report_annotations for select to authenticated using((select auth.uid())=(select public.amem_owner_uid())); end if;
 if to_regclass('storage.buckets') is not null then
 insert into storage.buckets(id,name,public,file_size_limit,allowed_mime_types)
 values('morning-audio','morning-audio',false,20000000,array['audio/mpeg']) on conflict(id) do nothing;
 end if;
end $$;
notify pgrst,'reload schema';
