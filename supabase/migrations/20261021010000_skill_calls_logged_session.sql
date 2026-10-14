-- Skill calls: the session id track.py logs (since 2026-10-14), so a call links to its session
-- exactly instead of by cwd + time. Nullable: older log lines have none. See docs/skills.md.
alter table public.skill_calls add column if not exists logged_session text;
create index if not exists skill_calls_logged_session_idx on public.skill_calls (logged_session)
    where session_id is null;
