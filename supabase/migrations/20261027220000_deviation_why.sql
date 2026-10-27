-- The writer's reason for a deviation as one label (docs/deviations.md, "Why category").
--
--   why_category   unclear | wrong | conflict | missing_context | tool_blocked | ignored
--                  the label the writer picked next to its free-text `why`; Jev's state gets the
--                  label, never the text. Null: scanned before this column existed.
--
-- Same values as deviations.WHY_CATEGORIES. RLS on `deviations` is unchanged (on, no policies,
-- no API-role grants); Night Desk's column grants on deviations do not include the new column.
alter table public.deviations add column if not exists why_category text;

alter table public.deviations drop constraint if exists deviations_why_category_check;
alter table public.deviations add constraint deviations_why_category_check check (
    why_category is null
    or why_category in ('unclear', 'wrong', 'conflict', 'missing_context', 'tool_blocked', 'ignored'));
