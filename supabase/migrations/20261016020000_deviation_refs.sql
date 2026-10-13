-- Stable rule ids for deviations (docs/deviations.md, "Rule catalog").
--   rule_id          the catalog id of the rule inside instruction_source (a slug of the
--                    heading path, e.g. safety-guards, explaining-completed-work/core-rule),
--                    'whole' for a file without headings, 'unmatched' when the writer's ref
--                    fit no rule. Null on rows from before this migration until `deviations reref`.
--   rule_title       the heading as written (a child: 'Parent > Child')
--   instruction_sha  sha256 of the source's files at scan time (null for backfilled rows),
--                    so a later step can tell deviations before and after an edit
--   meta             {"ref_match": "exact" | "snapped" | "unmatched", "ref_score": 0..1}
-- instruction_ref keeps the writer's own spelling. Recurrence and the report group by
-- instruction_source + rule_id (unmatched and old rows: the lowercased ref).
alter table public.deviations add column if not exists rule_id text;
alter table public.deviations add column if not exists rule_title text;
alter table public.deviations add column if not exists instruction_sha text
    check (instruction_sha is null or instruction_sha ~ '^[0-9a-f]{64}$');
alter table public.deviations add column if not exists meta jsonb not null default '{}'::jsonb;

create index if not exists deviations_rule_idx on public.deviations (instruction_source, rule_id);

-- RLS stays on (no policies, no API-role grants), as set in 20261016010000.
alter table public.deviations enable row level security;
