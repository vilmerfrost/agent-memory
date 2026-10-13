-- Cloud routine sessions (docs/cloud.md, "Cloud digests"). A cloud routine (claude.ai/code)
-- runs in a Linux VM; its transcript never reaches the Mac. As its last step the routine runs
-- tools/amem_digest.py and POSTs the row here through PostgREST:
--   POST /rest/v1/cloud_digests  Prefer: resolution=ignore-duplicates,return=minimal
-- `amem sessions ingest` turns each new row into a `sessions` row (tool claude, kind
-- routine, meta.cloud) and sets ingested_at; `amem deviations scan` reads the stored digest
-- and instruction texts instead of a local transcript.
--
--   source_key    cloud:<session id>, the primary key. PostgREST's ignore-duplicates
--                 resolves conflicts on the primary key unless `on_conflict` names another
--                 column, so a repeated POST is ignored (not an error, not a duplicate)
--                 with or without `?on_conflict=source_key`.
--   routine       the routine's slug (nightly-trials, fresh-install)
--   digest        the redacted, capped digest (deviations.build_digest); never the transcript
--   digest_sha    sha256 hex of the digest
--   instructions  [{name, sha256, text}]: `prompt` = the routine prompt, others = files
--                 such as NIGHTLY.md (redacted, capped text; sha256 of the file as read)
--   skills        skill names used
--   meta          {cloud, repo, script_version, transcript_bytes, tool_calls, prompts}
--   ingested_at   set by `amem sessions ingest` (null: not ingested yet)
create table if not exists public.cloud_digests (
    source_key   text primary key check (source_key ~ '^cloud:[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'),
    routine      text not null check (routine ~ '^[a-z0-9][a-z0-9._-]{0,79}$'),
    session_id   text not null,
    started_at   timestamptz,
    ended_at     timestamptz,
    digest       text not null default '',
    digest_sha   text check (digest_sha is null or digest_sha ~ '^[0-9a-f]{64}$'),
    instructions jsonb not null default '[]'::jsonb check (jsonb_typeof(instructions) = 'array'),
    skills       jsonb not null default '[]'::jsonb check (jsonb_typeof(skills) = 'array'),
    meta         jsonb not null default '{}'::jsonb check (jsonb_typeof(meta) = 'object'),
    ingested_at  timestamptz,
    created_at   timestamptz not null default now()
);

create index if not exists cloud_digests_pending_idx on public.cloud_digests (created_at) where ingested_at is null;

-- Same lockdown as the other tables: RLS on with no policies, no grants for the Supabase API
-- roles. The routine's POST carries the secret key (service role, bypasses RLS) through the
-- cloud network secret. The roles exist on Supabase but not on the plain test Postgres.
alter table public.cloud_digests enable row level security;

do $$
declare r text;
begin
  foreach r in array array['anon', 'authenticated'] loop
    if exists (select 1 from pg_roles where rolname = r) then
      execute format('revoke all on table public.cloud_digests from %I', r);
    end if;
  end loop;
end $$;
