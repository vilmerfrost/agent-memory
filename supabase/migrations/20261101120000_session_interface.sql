-- Which app a session ran in (docs/sessions-live.md, "Interface"). One new column on `sessions`;
-- nothing else changes (no rename, no drop, no type change, RLS unchanged).
--
-- Additive and nullable. Null means "not derived yet". No index: no query needs one. No check
-- constraint: a new value must never make a spooled ping fail.
alter table public.sessions add column if not exists interface text;

comment on column public.sessions.interface is
    'The app the session ran in: t3-code, claude-desktop, chatgpt-desktop, ide, cli, sdk, cloud or unknown. '
    'Derived by session_interface.derive from ingest, the live ping and amem sessions interface. Null: not derived yet.';
