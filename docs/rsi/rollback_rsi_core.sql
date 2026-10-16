-- Reverse supabase/migrations/20261022010000_rsi_core.sql (Mission 003). Manual only; never in migrations/.
-- It destroys the RSI audit trail (artifacts, evaluations, decisions). Export those tables first:
--   pg_dump "$AMEM_DB_URL" -t 'public.rsi_*' > rsi-audit-$(date +%Y%m%d).sql
-- Refuses while edits rows have status 'staged' (resolve or mark them first).
begin;

do $$
begin
  if exists (select 1 from public.edits where status = 'staged') then
    raise exception 'edits rows with status staged exist; resolve them before rolling back';
  end if;
end $$;

drop function if exists public.amem_rsi_record_decision(uuid, text, text, text);
drop function if exists public.amem_rsi_record_cli_decision(uuid, text, text, text);
drop function if exists public.rsi_insert_decision(public.rsi_proposals, text, text, text, text);
drop table if exists public.rsi_decision_results, public.rsi_decisions, public.rsi_decision_tokens,
    public.rsi_proposals, public.rsi_eval_results, public.rsi_artifacts, public.rsi_evidence,
    public.rsi_candidates, public.rsi_checkpoints, public.rsi_experiments cascade;
drop function if exists public.rsi_immutable();

alter table public.edits drop constraint if exists edits_status_check;
alter table public.edits add constraint edits_status_check check (status in ('proposed', 'kept', 'trial', 'auto-reverted',
    'refused', 'reverted', 'suggested', 'skipped'));

drop index if exists public.sessions_env_idx;
alter table public.sessions drop column if exists experiment_id;
alter table public.sessions drop column if exists env;

delete from public.schema_migrations where filename = '20261022010000_rsi_core.sql';

-- Roles are cluster-wide. Drop them when nothing else uses them; otherwise keep them (harmless, NOLOGIN).
do $$
declare r text;
begin
  foreach r in array array['rsi_reader', 'rsi_writer'] loop
    if exists (select 1 from pg_roles where rolname = r) then
      execute format('revoke all on schema public from %I', r);
      execute format('revoke all on all tables in schema public from %I', r);
      execute format('revoke all on all functions in schema public from %I', r);
      begin
        execute format('drop role %I', r);
      exception when dependent_objects_still_exist then
        raise notice 'kept role %: still used in another database', r;
      end;
    end if;
  end loop;
end $$;

commit;
