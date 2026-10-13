-- Codex sessions next to Claude Code sessions (docs/deviations.md, "Codex").
--   tool  which agent wrote the transcript: claude (~/.claude/projects) | codex (~/.codex/sessions)
-- Existing rows are Claude Code sessions, so the default fills them. Codex session ids
-- are `codex:<thread id>`. meta.team (bool) marks a team repo's session (no column: it is
-- a label the report reads, like meta.agent_type).
alter table public.sessions add column if not exists tool text not null default 'claude';

do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'sessions_tool_check') then
    alter table public.sessions add constraint sessions_tool_check check (tool in ('claude', 'codex'));
  end if;
end $$;

create index if not exists sessions_tool_idx on public.sessions (tool);

-- RLS stays on (no policies, no API-role grants), as set in 20261016010000.
alter table public.sessions enable row level security;
