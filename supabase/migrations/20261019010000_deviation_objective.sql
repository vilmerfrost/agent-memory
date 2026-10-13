-- Objective result grade per deviation (docs/deviations.md, "Objective result").
--
--   result_outcome     the session's outcome (sessions.outcome) when the deviation was stored,
--                      refreshed by `amem deviations reref` as outcomes change
--   objective_result   0..1 from that outcome: shipped 1.0, pushed 0.9, committed 0.8,
--                      no-change 0.7 (Q&A kinds only), partial 0.4, abandoned 0.1, failed 0.0;
--                      null for active / unknown (and no-change outside the Q&A kinds)
--
-- The writer's raw result_grade stays as it is. RLS on `deviations` is unchanged (on, no
-- policies, no API-role grants).
alter table public.deviations
    add column if not exists result_outcome text,
    add column if not exists objective_result double precision;

alter table public.deviations drop constraint if exists deviations_result_outcome_check;
alter table public.deviations add constraint deviations_result_outcome_check
    check (result_outcome is null or result_outcome in ('shipped', 'pushed', 'committed', 'partial', 'active',
                                                        'abandoned', 'failed', 'no-change'));
alter table public.deviations drop constraint if exists deviations_objective_result_check;
alter table public.deviations add constraint deviations_objective_result_check
    check (objective_result is null or (objective_result >= 0 and objective_result <= 1));
