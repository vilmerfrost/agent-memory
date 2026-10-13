-- Tokens per session and live session rows (docs/sessions-live.md).
--   tokens_input           uncached input tokens (Claude: usage.input_tokens; Codex:
--                          input_tokens - cached_input_tokens)
--   tokens_output          output tokens (Codex: includes reasoning tokens)
--   tokens_cache_read      input tokens read from the prompt cache
--   tokens_cache_creation  input tokens written to the prompt cache
--   models                 per-model breakdown {"<model>": {"input", "output", "cache_read",
--                          "cache_creation", "calls"}}
--   turns                  prompts a person typed (a worker's brief counts as one)
--   last_activity_at       the newest event time in the transcript, or the newest hook ping
--   tokens_updated_at      when the token columns were last written
-- Null token columns mean "not measured yet" (a row from before this migration, or a
-- SessionStart row with no answer yet). A subagent's tokens live on its own row (kind
-- worker); its parent's meta.subagent_tokens holds their sum, so a total over all rows
-- never counts a subagent twice.
alter table public.sessions add column if not exists tokens_input bigint;
alter table public.sessions add column if not exists tokens_output bigint;
alter table public.sessions add column if not exists tokens_cache_read bigint;
alter table public.sessions add column if not exists tokens_cache_creation bigint;
alter table public.sessions add column if not exists models jsonb;
alter table public.sessions add column if not exists turns int;
alter table public.sessions add column if not exists last_activity_at timestamptz;
alter table public.sessions add column if not exists tokens_updated_at timestamptz;

do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'sessions_models_object_check') then
    alter table public.sessions add constraint sessions_models_object_check
      check (models is null or jsonb_typeof(models) = 'object');
  end if;
  if not exists (select 1 from pg_constraint where conname = 'sessions_tokens_nonneg_check') then
    alter table public.sessions add constraint sessions_tokens_nonneg_check
      check (coalesce(tokens_input, 0) >= 0 and coalesce(tokens_output, 0) >= 0
             and coalesce(tokens_cache_read, 0) >= 0 and coalesce(tokens_cache_creation, 0) >= 0
             and coalesce(turns, 0) >= 0);
  end if;
end $$;

create index if not exists sessions_last_activity_idx on public.sessions (last_activity_at desc nulls last);

-- RLS stays on (no policies, no API-role grants), as set in 20261016010000.
alter table public.sessions enable row level security;

do $$
declare r text;
begin
  foreach r in array array['anon', 'authenticated'] loop
    if exists (select 1 from pg_roles where rolname = r) then
      execute format('revoke all on table public.sessions from %I', r);
    end if;
  end loop;
end $$;
