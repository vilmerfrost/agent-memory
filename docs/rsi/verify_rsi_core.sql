-- Post-migration check for supabase/migrations/20261022010000_rsi_core.sql (Mission 003.5, Phase C).
-- Leaves nothing behind: every write happens in one transaction that ends in ROLLBACK.
-- Raises on the first failure; prints NOTICE 'rsi_core verify: OK' when every check passes.
begin;

do $$
declare
  t text; r text; n int; h text; canon text; pid uuid; res text;
  tok constant text := 'verify-token-0123456789abcdef';
begin
  -- 1. Objects
  select count(*) into n from pg_tables where schemaname = 'public' and tablename like 'rsi\_%';
  if n <> 10 then raise exception 'expected 10 rsi_* tables, found %', n; end if;
  select count(*) into n from information_schema.columns
   where table_schema = 'public' and table_name = 'sessions' and column_name in ('env', 'experiment_id');
  if n <> 2 then raise exception 'sessions.env / experiment_id missing'; end if;
  if not exists (select 1 from public.schema_migrations where filename = '20261022010000_rsi_core.sql') then
    raise exception 'migration not recorded in schema_migrations';
  end if;
  select count(*) into n from pg_class c join pg_namespace s on s.oid = c.relnamespace
   where s.nspname = 'public' and c.relname like 'rsi\_%' and c.relkind = 'r' and not c.relrowsecurity;
  if n > 0 then raise exception '% rsi_* tables without RLS', n; end if;

  -- 2. API roles: nothing on the RSI tables; only service_role may run the phone function
  foreach r in array array['anon', 'authenticated', 'service_role'] loop
    if exists (select 1 from pg_roles where rolname = r) then
      for t in select tablename from pg_tables where schemaname = 'public' and tablename like 'rsi\_%' loop
        if has_table_privilege(r, 'public.' || t, 'select') or has_table_privilege(r, 'public.' || t, 'insert')
           or has_table_privilege(r, 'public.' || t, 'update') or has_table_privilege(r, 'public.' || t, 'delete')
           or has_table_privilege(r, 'public.' || t, 'truncate') then
          raise exception '% has privileges on %', r, t;
        end if;
      end loop;
      if has_function_privilege(r, 'public.amem_rsi_record_cli_decision(uuid,text,text,text)', 'execute') then
        raise exception '% can record CLI decisions', r;
      end if;
      if has_function_privilege(r, 'public.rsi_insert_decision(public.rsi_proposals,text,text,text,text)', 'execute') then
        raise exception '% can execute rsi_insert_decision', r;
      end if;
      if has_function_privilege(r, 'public.amem_rsi_record_decision(uuid,text,text,text)', 'execute') <> (r = 'service_role') then
        raise exception 'phone decision function: wrong execute right for %', r;
      end if;
    end if;
  end loop;
  foreach r in array array['rsi_reader', 'rsi_writer'] loop
    if has_table_privilege(r, 'public.rsi_decisions', 'insert')
       or has_function_privilege(r, 'public.amem_rsi_record_decision(uuid,text,text,text)', 'execute')
       or has_function_privilege(r, 'public.amem_rsi_record_cli_decision(uuid,text,text,text)', 'execute') then
      raise exception '% has a decision path', r;
    end if;
  end loop;

  -- 3. Disposable decision flow (rolled back below)
  canon := '{"activation":"none","baseline_id":"bl-verify000000","experiment_id":"exp-verify-0000","files":[],'
           '"format":"rsi-artifact/1","proposal_kind":"edit_instruction"}';
  h := encode(sha256(convert_to(canon, 'UTF8')), 'hex');
  insert into public.rsi_artifacts (hash, manifest_canonical, manifest, files, experiment_id, baseline_id)
  values (h, canon, canon::jsonb, '{}'::jsonb, 'exp-verify-0000', 'bl-verify000000');
  insert into public.rsi_proposals (artifact_hash, title, targets)
  values (h, 'verify_rsi_core (rolled back)', array['~/verify.md']) returning id into pid;
  insert into public.rsi_decision_tokens (proposal_id, action, token_hash, expires_at)
  values (pid, 'apply', encode(sha256(convert_to(tok || ':' || pid::text || ':' || h || ':apply', 'UTF8')), 'hex'),
          now() + interval '1 hour');
  res := public.amem_rsi_record_decision(pid, 'apply', h, 'wrong-token-0123456789');
  if res <> 'denied' then raise exception 'a wrong token gave %', res; end if;
  res := public.amem_rsi_record_decision(pid, 'apply', repeat('f', 64), tok);
  if res <> 'hash-mismatch' then raise exception 'a wrong artifact hash gave %', res; end if;
  res := public.amem_rsi_record_decision(pid, 'apply', h, tok);
  if res <> 'recorded' then raise exception 'the valid token gave %', res; end if;
  res := public.amem_rsi_record_decision(pid, 'apply', h, tok);
  if res <> 'already' then raise exception 'a replayed token gave %', res; end if;
  if (select status from public.rsi_proposals where id = pid) <> 'approved' then
    raise exception 'proposal not approved after a recorded decision';
  end if;
  begin
    update public.rsi_decisions set reason = 'tamper' where proposal_id = pid;
    raise exception 'tamper allowed';
  exception when raise_exception then
    if sqlerrm not like '%append-only%' then raise; end if;
  end;
  raise notice 'rsi_core verify: OK';
end $$;

rollback;
