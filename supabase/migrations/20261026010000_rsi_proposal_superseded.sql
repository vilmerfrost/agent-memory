-- A proposal can be `superseded` (2026-10-26): the morning edits step (amem-daily, AMEM_EDITS_MODE=stage) no longer
-- runs, because the RSI night has its own writer. `amem rsi supersede-morning` moves the morning step's proposals
-- that still wait (status staged) to `superseded`, so Night Desk stops listing them as waiting. Rows are never
-- deleted; their decision functions answer `not-staged`, so an old Apply or Reject button does nothing.
alter table public.rsi_proposals drop constraint if exists rsi_proposals_status_check;
alter table public.rsi_proposals add constraint rsi_proposals_status_check
    check (status in ('staged', 'approved', 'rejected', 'expired', 'stale', 'promoting', 'promoted', 'failed',
                      'reverted', 'superseded'));

-- RLS on rsi_proposals is unchanged (on, policies from 20261022010000_rsi_core.sql).
alter table public.rsi_proposals enable row level security;

notify pgrst, 'reload schema';
