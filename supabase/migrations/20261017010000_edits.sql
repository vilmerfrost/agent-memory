-- The edit step (docs/edits.md, roadmap v2 E4 + E5). One row per instruction edit the loop
-- tried, suggested or skipped, and one row per undo request (the phone's Undo button).
-- The file change itself is agent-config's `improve apply` (one commit, `improve revert` undoes).

-- edits
--   improve_id             the change id from `improve apply` (null: not applied / suggested)
--   status                 proposed       row written, improve not answered yet (or garbage reply)
--                          kept | trial   improve kept the change (trial: evals could not prove it)
--                          auto-reverted  improve's evals reverted it
--                          refused        improve refused it (scope, pin, cap, content guard)
--                          reverted       undone later (phone Undo or `amem edits undo`)
--                          suggested      not editable here: logged with `improve suggest`
--                          skipped        the writer proposed no change
--   target_path            ~-relative file the edit targets (the real file, symlinks resolved)
--   instruction_sha_before sha256 of the file before the edit (rule_catalog.sha), the dedupe key
--   instruction_sha_after  sha256 after (kept / trial only)
--   deviation_ids          the deviations that motivated it
--   undo_token_hash        sha256 hex of the per-edit undo token; the token itself is never stored
create table if not exists public.edits (
    id                     uuid primary key default gen_random_uuid(),
    improve_id             text,
    status                 text not null check (status in ('proposed', 'kept', 'trial', 'auto-reverted',
                                                           'refused', 'reverted', 'suggested', 'skipped')),
    target_path            text,
    instruction_source     text not null,
    rule_id                text not null,
    rule_title             text,
    instruction_sha_before text check (instruction_sha_before is null or instruction_sha_before ~ '^[0-9a-f]{64}$'),
    instruction_sha_after  text check (instruction_sha_after is null or instruction_sha_after ~ '^[0-9a-f]{64}$'),
    deviation_ids          uuid[] not null default '{}',
    verdict                text check (verdict is null or verdict in ('edit_instruction', 'enforce', 'leave')),
    jev_confidence         real check (jev_confidence is null or (jev_confidence >= 0 and jev_confidence <= 1)),
    why                    text,
    summary                text,
    pushed_at              timestamptz,
    undo_token_hash        text check (undo_token_hash is null or undo_token_hash ~ '^[0-9a-f]{64}$'),
    undo_requested_at      timestamptz,
    reverted_at            timestamptz,
    meta                   jsonb not null default '{}'::jsonb,
    created_at             timestamptz not null default now()
);

create index if not exists edits_rule_idx on public.edits (instruction_source, rule_id);
create index if not exists edits_created_idx on public.edits (created_at desc);

-- undo_requests: one per edit (the edge function inserts it idempotently).
--   source     phone (edge function amem-undo) | cli (`amem edits undo`)
--   handled_at set by `amem edits undo-poll` once the revert ran (or there was nothing to revert)
--   result     reverted | nothing to revert (...) | failed: ...
--   attempts   revert attempts so far; after 3 failures the request is closed as failed
create table if not exists public.undo_requests (
    id           uuid primary key default gen_random_uuid(),
    edit_id      uuid not null unique references public.edits (id) on delete cascade,
    requested_at timestamptz not null default now(),
    source       text not null default 'phone' check (source in ('phone', 'cli')),
    handled_at   timestamptz,
    result       text,
    attempts     int not null default 0
);

create index if not exists undo_requests_pending_idx on public.undo_requests (requested_at) where handled_at is null;

-- Same lockdown as the other tables: RLS on with no policies, no grants for the Supabase API
-- roles. The edge function uses the service role (bypasses RLS). The roles exist on Supabase
-- but not on the plain test Postgres.
alter table public.edits enable row level security;
alter table public.undo_requests enable row level security;

do $$
declare r text;
begin
  foreach r in array array['anon', 'authenticated'] loop
    if exists (select 1 from pg_roles where rolname = r) then
      execute format('revoke all on table public.edits from %I', r);
      execute format('revoke all on table public.undo_requests from %I', r);
    end if;
  end loop;
end $$;
