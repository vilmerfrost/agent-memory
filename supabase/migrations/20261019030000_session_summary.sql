-- Session summaries (docs/sessions-live.md, "Summary"). Two new columns on `sessions`;
-- nothing else changes (no rename, no drop, no type change, RLS unchanged).
--
--   summary             1 to 3 plain sentences: what was changed or decided, which files or
--                       areas, whether it finished. Written by a cheap model (Haiku) from the
--                       redacted transcript tail plus outcome_evidence; redacted, max 600
--                       chars (capped in code). Null: not summarized (yet).
--   summary_updated_at  when `summary` was last written; a row whose last_activity_at is
--                       newer gets a new summary.
--
-- The commits a session made go into the existing `outcome_evidence` jsonb as
-- `commits: [{sha, message, repo, branch}]` (no schema change).
alter table public.sessions
    add column if not exists summary text,
    add column if not exists summary_updated_at timestamptz;
