-- Post-migration check for 20261023010000_amem_app_role.sql + 20261023020000_rsi_totp.sql (Mission 003.5, Phase E).
-- Leaves nothing behind: every write happens in one transaction that ends in ROLLBACK.
-- Raises on the first failure; prints NOTICE 'least_privilege verify: OK' when every check passes.
begin;

do $$
declare
  r text; t text; n int; h text; canon text; p1 uuid; p2 uuid; res text;
  tok constant text := 'verify-token-0123456789abcdef';
  th1 text; th2 text;
begin
  -- 1. Objects
  if not exists (select 1 from pg_roles where rolname = 'amem_app' and rolcanlogin) then
    raise exception 'role amem_app missing or cannot log in';
  end if;
  if not pg_has_role('amem_app', 'rsi_writer', 'member') then raise exception 'amem_app is not a member of rsi_writer'; end if;
  foreach t in array array['rsi_totp_used', 'rsi_totp_failures'] loop
    if not exists (select 1 from pg_tables where schemaname = 'public' and tablename = t) then
      raise exception '% missing', t;
    end if;
  end loop;

  -- 2. No decision path for amem_app or the automation roles; API roles reach only the TOTP-gated functions
  foreach r in array array['amem_app', 'rsi_writer', 'rsi_reader'] loop
    if has_table_privilege(r, 'public.rsi_decisions', 'insert') or has_table_privilege(r, 'public.rsi_decision_tokens', 'insert')
       or has_table_privilege(r, 'public.rsi_decision_tokens', 'update')
       or has_function_privilege(r, 'public.amem_rsi_record_decision(uuid,text,text,text)', 'execute')
       or has_function_privilege(r, 'public.amem_rsi_record_cli_decision(uuid,text,text,text)', 'execute')
       or has_function_privilege(r, 'public.amem_rsi_record_phone_decision(uuid,text,text,text,bigint)', 'execute')
       or has_function_privilege(r, 'public.amem_rsi_phone_bad_code(uuid)', 'execute')
       or has_table_privilege(r, 'public.rsi_totp_used', 'select') then
      raise exception '% has a decision path', r;
    end if;
  end loop;
  if not has_function_privilege('amem_app', 'public.amem_rsi_arm_token(uuid,text,text,interval)', 'execute') then
    raise exception 'amem_app cannot arm tokens (the push would fail)';
  end if;
  for t in select tablename from pg_tables where schemaname = 'public' and tablename not like 'rsi\_%' and tablename <> 'night_reads' loop
    if not has_table_privilege('amem_app', 'public.' || t, 'select') then raise exception 'amem_app cannot read %', t; end if;
  end loop;
  -- Read receipts are UI state, not an ingestion table. Automation must not read them.
  if to_regclass('public.night_reads') is not null then
    if has_table_privilege('amem_app', 'public.night_reads', 'select') then
      raise exception 'amem_app can read owner-only night read status';
    end if;
  end if;
  foreach r in array array['anon', 'authenticated', 'service_role'] loop
    if exists (select 1 from pg_roles where rolname = r) then
      if has_function_privilege(r, 'public.amem_rsi_record_decision(uuid,text,text,text)', 'execute')
         or has_function_privilege(r, 'public.amem_rsi_record_cli_decision(uuid,text,text,text)', 'execute')
         or has_function_privilege(r, 'public.amem_rsi_arm_token(uuid,text,text,interval)', 'execute') then
        raise exception '% can bypass the TOTP gate', r;
      end if;
      if has_function_privilege(r, 'public.amem_rsi_record_phone_decision(uuid,text,text,text,bigint)', 'execute') <> (r = 'service_role')
         or has_function_privilege(r, 'public.amem_rsi_phone_bad_code(uuid)', 'execute') <> (r = 'service_role') then
        raise exception 'TOTP functions: wrong execute right for %', r;
      end if;
      for t in select tablename from pg_tables where schemaname = 'public' and tablename like 'rsi\_%' loop
        if has_table_privilege(r, 'public.' || t, 'select') or has_table_privilege(r, 'public.' || t, 'insert') then
          raise exception '% has privileges on %', r, t;
        end if;
      end loop;
    end if;
  end loop;

  -- 3. Disposable TOTP flow (rolled back below)
  canon := '{"activation":"none","baseline_id":"bl-verify000000","experiment_id":"exp-verify-0001","files":[],'
           '"format":"rsi-artifact/1","proposal_kind":"edit_instruction"}';
  h := encode(sha256(convert_to(canon, 'UTF8')), 'hex');
  insert into public.rsi_artifacts (hash, manifest_canonical, manifest, files, experiment_id, baseline_id)
  values (h, canon, canon::jsonb, '{}'::jsonb, 'exp-verify-0001', 'bl-verify000000');
  insert into public.rsi_proposals (artifact_hash, title, targets) values (h, 'verify LP 1 (rolled back)', array['~/v1.md'])
  returning id into p1;
  th1 := encode(sha256(convert_to(tok || ':' || p1::text || ':' || h || ':apply', 'UTF8')), 'hex');
  if not public.amem_rsi_arm_token(p1, 'apply', th1, interval '1 hour') then raise exception 'arm_token refused a new token'; end if;
  res := public.amem_rsi_record_phone_decision(p1, 'apply', h, tok, null);
  if res <> 'totp-required' then raise exception 'apply without a code gave %', res; end if;
  res := public.amem_rsi_record_phone_decision(p1, 'apply', h, tok, 9000000001);
  if res <> 'recorded' then raise exception 'apply with a step gave %', res; end if;
  if public.amem_rsi_arm_token(p1, 'apply', th1, interval '1 hour') then raise exception 'a used token was re-armed'; end if;
  update public.rsi_proposals set status = 'rejected' where id = p1;            -- free the artifact for a second proposal
  insert into public.rsi_proposals (artifact_hash, title, targets) values (h, 'verify LP 2 (rolled back)', array['~/v2.md'])
  returning id into p2;
  th2 := encode(sha256(convert_to(tok || ':' || p2::text || ':' || h || ':apply', 'UTF8')), 'hex');
  perform public.amem_rsi_arm_token(p2, 'apply', th2, interval '1 hour');
  res := public.amem_rsi_record_phone_decision(p2, 'apply', h, tok, 9000000001);
  if res <> 'totp-replayed' then raise exception 'a reused step gave %', res; end if;
  for n in 1..5 loop perform public.amem_rsi_phone_bad_code(p2); end loop;
  res := public.amem_rsi_record_phone_decision(p2, 'apply', h, tok, 9000000002);
  if res <> 'locked' then raise exception 'after 5 wrong codes: %', res; end if;
  raise notice 'least_privilege verify: OK';
end $$;

rollback;
