-- Files the cloud setup script downloads (docs/cloud.md, "Cloud chats"). `amem cloud
-- publish-script` upserts tools/amem_digest.py here; deploy/cloud/setup.sh fetches it with
--   GET /rest/v1/amem_assets?name=eq.amem_digest.py&select=body
-- through the cloud proxy (the network secret adds the apikey: service role, bypasses RLS).
--
--   name        file name, the primary key (amem_digest.py)
--   body        the file text
--   sha256      sha256 hex of body
--   version     the file's own version (SCRIPT_VERSION for amem_digest.py), or null
--   updated_at  last publish that changed the body
create table if not exists public.amem_assets (
    name       text primary key check (name ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$'),
    body       text not null,
    sha256     text not null check (sha256 ~ '^[0-9a-f]{64}$'),
    version    text,
    updated_at timestamptz not null default now()
);

-- Same lockdown as the other tables: RLS on with no policies, no grants for the API roles.
alter table public.amem_assets enable row level security;

do $$
declare r text;
begin
  foreach r in array array['anon', 'authenticated'] loop
    if exists (select 1 from pg_roles where rolname = r) then
      execute format('revoke all on table public.amem_assets from %I', r);
    end if;
  end loop;
end $$;
