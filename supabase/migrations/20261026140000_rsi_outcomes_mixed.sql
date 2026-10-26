-- rsi_outcomes.verdict gains 'mixed': the rule's own text changed again inside the measuring windows (a hand edit or
-- another change), so the result is neither credited nor blamed on the change (docs/rsi/outcome.md).
alter table public.rsi_outcomes drop constraint if exists rsi_outcomes_verdict_check;
alter table public.rsi_outcomes add constraint rsi_outcomes_verdict_check
    check (verdict in ('helped', 'no-change', 'worse', 'too-early', 'mixed'));
