-- Web Apply / Reject / Undo from Night Desk (docs/rsi/approval.md "Web decisions", Vilmer 2026-10-25).
-- The edge function amem-decide checks the owner's Supabase login, the Origin and (Apply / Undo) a WebAuthn
-- passkey assertion (Face ID). It then calls the functions below as service_role. They record a decision or an
-- undo request only; they never promote. The promotion worker (`amem rsi decide-poll`) is unchanged.
--
-- Tables (owner only: no grants to the API roles, amem_app or the automation roles):
--   rsi_passkeys              enrolled passkeys (credential id, COSE public key, sign count); revoked, never deleted
--   rsi_passkey_enroll_codes  one-use enrollment codes from `amem rsi passkey-enroll` (sha256 only, 10 min)
--   rsi_web_challenges        one-use challenges (sha256 of the WebAuthn challenge only, 2 min), each bound to
--                             (kind, id, action, artifact_hash)
-- Decisions keep their append-only trigger. New columns: channel (generated from source: web | phone | cli)
-- and passkey_id (the credential that approved a web Apply / Undo). Web decisions keep verified = 'edge-token'
-- because agent-config's `improve promote` accepts only edge-token | cli-tty: the edge function did verify it.

-- ---------------------------------------------------------------------------------------------- tables
create table if not exists public.rsi_passkeys (
    id            uuid primary key default gen_random_uuid(),
    credential_id text not null unique
                  check (credential_id ~ '^[A-Za-z0-9_-]+$' and length(credential_id) between 16 and 1400),
    public_key    text not null                                                    -- base64url COSE key
                  check (public_key ~ '^[A-Za-z0-9_-]+$' and length(public_key) between 16 and 4000),
    sign_count    bigint not null default 0 check (sign_count >= 0),
    transports    text[] not null default '{}',
    device_type   text,
    backed_up     boolean,
    created_at    timestamptz not null default now(),
    last_used_at  timestamptz,
    revoked_at    timestamptz
);

create table if not exists public.rsi_passkey_enroll_codes (
    code_hash  text primary key check (code_hash ~ '^[0-9a-f]{64}$'),
    created_at timestamptz not null default now(),
    expires_at timestamptz not null,
    used_at    timestamptz,
    passkey_id uuid references public.rsi_passkeys (id)
);

create table if not exists public.rsi_web_challenges (
    id               uuid primary key default gen_random_uuid(),
    nonce_hash       text not null unique check (nonce_hash ~ '^[0-9a-f]{64}$'),
    kind             text not null check (kind in ('proposal', 'edit', 'enroll')),
    target_id        uuid,
    action           text not null check (action in ('apply', 'reject', 'undo', 'enroll')),
    artifact_hash    text check (artifact_hash is null or artifact_hash ~ '^[0-9a-f]{64}$'),
    enroll_code_hash text references public.rsi_passkey_enroll_codes (code_hash),
    created_at       timestamptz not null default now(),
    expires_at       timestamptz not null,
    used_at          timestamptz,
    result           text,
    check ((kind = 'proposal' and action in ('apply', 'reject', 'undo') and target_id is not null and artifact_hash is not null)
        or (kind = 'edit' and action = 'undo' and target_id is not null and artifact_hash is null)
        or (kind = 'enroll' and action = 'enroll' and target_id is null and enroll_code_hash is not null))
);
create index if not exists rsi_web_challenges_expires_idx on public.rsi_web_challenges (expires_at);

alter table public.rsi_passkeys enable row level security;
alter table public.rsi_passkey_enroll_codes enable row level security;
alter table public.rsi_web_challenges enable row level security;

-- ------------------------------------------------------------------------------------- decisions: channel
-- ALTER TABLE does not fire the row triggers, so the append-only guard stays intact for every row.
alter table public.rsi_decisions drop constraint if exists rsi_decisions_source_check;
alter table public.rsi_decisions add constraint rsi_decisions_source_check check (source in ('phone', 'cli', 'web'));
alter table public.rsi_decisions add column if not exists channel text generated always as (source) stored;
alter table public.rsi_decisions add column if not exists passkey_id uuid references public.rsi_passkeys (id);

alter table public.undo_requests drop constraint if exists undo_requests_source_check;
alter table public.undo_requests add constraint undo_requests_source_check check (source in ('phone', 'cli', 'web'));

-- ------------------------------------------------------------------------- the shared insert, with a passkey
-- Same rules as rsi_insert_decision (20261022010000); the 5-argument version now calls this one.
create or replace function public.rsi_insert_decision(pr public.rsi_proposals, p_action text, p_source text,
                                                     p_verified text, p_reason text, p_passkey uuid) returns text
language plpgsql set search_path = public, pg_temp as $$
begin
  if p_action in ('apply', 'reject') then
    if pr.status <> 'staged' then
      return 'not-staged';
    end if;
    if pr.expires_at <= now() then
      update public.rsi_proposals set status = 'expired' where id = pr.id;
      return 'expired';
    end if;
  elsif pr.status <> 'promoted' then
    return 'not-promoted';
  end if;
  if exists (select 1 from public.rsi_decisions where proposal_id = pr.id and action = p_action) then
    return 'already';
  end if;
  -- apply and reject exclude each other: one decision consumes both tokens
  update public.rsi_decision_tokens set used_at = coalesce(used_at, now())
   where proposal_id = pr.id and (action = p_action or (p_action in ('apply', 'reject') and action in ('apply', 'reject')));
  insert into public.rsi_decisions (proposal_id, action, artifact_hash, source, verified, reason, expires_at, passkey_id)
  values (pr.id, p_action, pr.artifact_hash, p_source, p_verified, p_reason, now() + interval '24 hours', p_passkey);
  update public.rsi_proposals
     set status = case p_action when 'apply' then 'approved' when 'reject' then 'rejected' else status end,
         decided_at = coalesce(decided_at, now())
   where id = pr.id;
  return 'recorded';
end $$;

create or replace function public.rsi_insert_decision(pr public.rsi_proposals, p_action text, p_source text,
                                                     p_verified text, p_reason text) returns text
language sql set search_path = public, pg_temp as $$
  select public.rsi_insert_decision(pr, p_action, p_source, p_verified, p_reason, null::uuid)
$$;

-- ------------------------------------------------------------------------------------------- challenges
-- Issue a challenge for one action. p_nonce_hash = sha256 hex of the base64url WebAuthn challenge (the edge
-- function keeps the challenge itself only in its reply). Returns {result: ok | invalid | missing | not-staged |
-- expired | not-promoted | not-enrolled, challenge_id, expires_at, needs_passkey, credentials: [{id, transports}],
-- summary}. The early refusals save a pointless Face ID; amem_rsi_web_decide checks everything again.
create or replace function public.amem_rsi_web_challenge(p_kind text, p_id uuid, p_action text, p_nonce_hash text)
returns jsonb
language plpgsql security definer set search_path = public, pg_temp as $$
declare
  pr public.rsi_proposals%rowtype;
  ed record;
  needs boolean := p_action in ('apply', 'undo');
  creds jsonb;
  cid uuid;
  exp timestamptz := now() + interval '2 minutes';
  summary jsonb;
begin
  if p_id is null or p_nonce_hash is null or p_nonce_hash !~ '^[0-9a-f]{64}$'
     or not ((p_kind = 'proposal' and p_action in ('apply', 'reject', 'undo')) or (p_kind = 'edit' and p_action = 'undo')) then
    return jsonb_build_object('result', 'invalid');
  end if;
  if p_kind = 'proposal' then
    select * into pr from public.rsi_proposals where id = p_id;
    if not found then
      return jsonb_build_object('result', 'missing');
    end if;
    if p_action in ('apply', 'reject') and pr.status <> 'staged' then
      return jsonb_build_object('result', 'not-staged');
    end if;
    if p_action in ('apply', 'reject') and pr.expires_at <= now() then
      return jsonb_build_object('result', 'expired');
    end if;
    if p_action = 'undo' and pr.status <> 'promoted' then
      return jsonb_build_object('result', 'not-promoted');
    end if;
    summary := jsonb_build_object('kind', p_kind, 'id', p_id, 'action', p_action,
                                  'target', array_to_string(pr.targets, ', '), 'title', pr.title,
                                  'artifact_hash', pr.artifact_hash, 'state', pr.status);
  else
    select id, status, target_path, rule_title, improve_id into ed from public.edits where id = p_id;
    if not found then
      return jsonb_build_object('result', 'missing');
    end if;
    if ed.status not in ('kept', 'trial') or ed.improve_id is null then
      return jsonb_build_object('result', 'not-promoted');
    end if;
    summary := jsonb_build_object('kind', p_kind, 'id', p_id, 'action', p_action, 'target', ed.target_path,
                                  'title', ed.rule_title, 'artifact_hash', null, 'state', ed.status);
  end if;
  select coalesce(jsonb_agg(jsonb_build_object('id', credential_id, 'transports', to_jsonb(transports))
                            order by created_at), '[]'::jsonb)
    into creds from public.rsi_passkeys where revoked_at is null;
  if needs and jsonb_array_length(creds) = 0 then
    return jsonb_build_object('result', 'not-enrolled');
  end if;
  delete from public.rsi_web_challenges where expires_at < now() - interval '7 days';   -- housekeeping
  insert into public.rsi_web_challenges (nonce_hash, kind, target_id, action, artifact_hash, expires_at)
  values (p_nonce_hash, p_kind, p_id, p_action, case when p_kind = 'proposal' then pr.artifact_hash end, exp)
  returning id into cid;
  return jsonb_build_object('result', 'ok', 'challenge_id', cid, 'expires_at', exp, 'needs_passkey', needs,
                            'credentials', case when needs then creds else '[]'::jsonb end, 'summary', summary);
end $$;

-- What the edge function needs before it verifies an assertion or attestation: the challenge (never the
-- nonce) and, for an assertion, the stored credential. Read-only.
create or replace function public.amem_rsi_web_context(p_challenge uuid, p_credential_id text) returns jsonb
language plpgsql security definer set search_path = public, pg_temp as $$
declare
  c public.rsi_web_challenges%rowtype;
  k public.rsi_passkeys%rowtype;
begin
  select * into c from public.rsi_web_challenges where id = p_challenge;
  if not found then
    return jsonb_build_object('found', false);
  end if;
  if p_credential_id is not null then
    select * into k from public.rsi_passkeys where credential_id = p_credential_id and revoked_at is null;
  end if;
  return jsonb_build_object(
    'found', true, 'kind', c.kind, 'action', c.action, 'nonce_hash', c.nonce_hash,
    'expired', c.expires_at <= now(), 'used', c.used_at is not null,
    'credential', case when k.id is null then null else
      jsonb_build_object('id', k.credential_id, 'public_key', k.public_key, 'sign_count', k.sign_count,
                         'transports', to_jsonb(k.transports)) end);
end $$;

-- ------------------------------------------------------------------------------------------- decisions
-- Record one web decision. The edge function has already checked the owner's login and Origin and, for
-- Apply / Undo, verified the passkey assertion over the challenge whose sha256 is p_nonce_hash, with the
-- authenticator's sign count p_sign_count. Here, in one transaction: the challenge (one use, 2 min, bound to
-- kind / id / action / artifact hash), the credential (enrolled, not revoked, sign count), then the same state
-- and hash checks as the phone path. A second call for the same decision returns 'already'.
-- Returns: recorded | already | denied | expired | missing | hash-mismatch | not-staged | not-promoted | invalid |
--          passkey-required | passkey-invalid | not-enrolled
create or replace function public.amem_rsi_web_decide(p_challenge uuid, p_nonce_hash text, p_credential_id text,
                                                      p_sign_count bigint) returns text
language plpgsql security definer set search_path = public, pg_temp as $$
declare
  c public.rsi_web_challenges%rowtype;
  k public.rsi_passkeys%rowtype;
  pr public.rsi_proposals%rowtype;
  needs boolean;
  res text;
  u text;
  ur record;
  edit_status text;
begin
  select * into c from public.rsi_web_challenges where id = p_challenge for update;
  if not found or c.kind = 'enroll' then
    return 'invalid';
  end if;
  needs := c.action in ('apply', 'undo');
  if c.used_at is not null then
    -- A retried or replayed request: say 'already' when this exact decision exists, never record again.
    if c.kind = 'proposal' and exists (select 1 from public.rsi_decisions where proposal_id = c.target_id and action = c.action) then
      return 'already';
    end if;
    if c.kind = 'edit' and c.result in ('recorded', 'already') then
      return 'already';
    end if;
    return 'expired';
  end if;
  if c.expires_at <= now() then
    return 'expired';
  end if;
  if needs then
    if p_credential_id is null or p_nonce_hash is null then
      return 'passkey-required';                       -- nothing consumed: the client can still sign
    end if;
    if p_nonce_hash <> c.nonce_hash then
      update public.rsi_web_challenges set used_at = now(), result = 'passkey-invalid' where id = c.id;
      return 'passkey-invalid';
    end if;
    select * into k from public.rsi_passkeys where credential_id = p_credential_id for update;
    if not found or k.revoked_at is not null then
      update public.rsi_web_challenges set used_at = now(), result = 'not-enrolled' where id = c.id;
      return 'not-enrolled';
    end if;
    if p_sign_count is null or p_sign_count < 0
       or ((p_sign_count > 0 or k.sign_count > 0) and p_sign_count <= k.sign_count) then
      update public.rsi_web_challenges set used_at = now(), result = 'passkey-invalid' where id = c.id;
      return 'passkey-invalid';                          -- a cloned or replayed authenticator
    end if;
    update public.rsi_passkeys set sign_count = p_sign_count, last_used_at = now() where id = k.id;
  end if;
  update public.rsi_web_challenges set used_at = now() where id = c.id;

  if c.kind = 'proposal' then
    select * into pr from public.rsi_proposals where id = c.target_id for update;
    if not found then
      res := 'missing';
    elsif pr.artifact_hash is distinct from c.artifact_hash then
      res := 'hash-mismatch';
    elsif exists (select 1 from public.rsi_decisions where proposal_id = pr.id and action = c.action) then
      res := 'already';
    else
      res := public.rsi_insert_decision(pr, c.action, 'web', 'edge-token', null, k.id);
    end if;
  else
    -- The older edit loop: the existing undo request path; amem-undo-poll stays the executor.
    select status into edit_status from public.edits where id = c.target_id for update;
    if not found then
      res := 'missing';
    else
      select handled_at, result into ur from public.undo_requests where edit_id = c.target_id;
      if edit_status = 'reverted' or (found and (ur.handled_at is null or ur.result = 'reverted')) then
        res := 'already';
      else
        u := public.amem_request_undo(c.target_id, 'web');
        res := case u when 'queued' then 'recorded' when 'reverted' then 'already'
                      when 'nothing' then 'not-promoted' when 'missing' then 'missing' else 'invalid' end;
      end if;
    end if;
  end if;
  update public.rsi_web_challenges set result = res where id = c.id;
  return res;
end $$;

-- ------------------------------------------------------------------------------------------- enrollment
-- Start an enrollment with a code from `amem rsi passkey-enroll`. The code is used up only by a successful
-- amem_rsi_web_enroll_finish. Returns {result: ok | invalid | expired, challenge_id, expires_at, exclude: [ids]}.
create or replace function public.amem_rsi_web_enroll_start(p_code_hash text, p_nonce_hash text) returns jsonb
language plpgsql security definer set search_path = public, pg_temp as $$
declare
  e public.rsi_passkey_enroll_codes%rowtype;
  cid uuid;
  exp timestamptz;
  ex jsonb;
begin
  if p_code_hash is null or p_code_hash !~ '^[0-9a-f]{64}$' or p_nonce_hash is null or p_nonce_hash !~ '^[0-9a-f]{64}$' then
    return jsonb_build_object('result', 'invalid');
  end if;
  select * into e from public.rsi_passkey_enroll_codes where code_hash = p_code_hash for update;
  if not found or e.used_at is not null then
    return jsonb_build_object('result', 'invalid');
  end if;
  if e.expires_at <= now() then
    return jsonb_build_object('result', 'expired');
  end if;
  exp := least(now() + interval '5 minutes', e.expires_at);
  insert into public.rsi_web_challenges (nonce_hash, kind, action, enroll_code_hash, expires_at)
  values (p_nonce_hash, 'enroll', 'enroll', e.code_hash, exp) returning id into cid;
  select coalesce(jsonb_agg(credential_id order by created_at), '[]'::jsonb) into ex
    from public.rsi_passkeys where revoked_at is null;
  return jsonb_build_object('result', 'ok', 'challenge_id', cid, 'expires_at', exp, 'exclude', ex);
end $$;

-- Store the verified credential and use up the code. Returns enrolled | invalid | expired.
create or replace function public.amem_rsi_web_enroll_finish(p_challenge uuid, p_nonce_hash text, p_credential_id text,
                                                             p_public_key text, p_sign_count bigint, p_transports text[],
                                                             p_device_type text, p_backed_up boolean) returns text
language plpgsql security definer set search_path = public, pg_temp as $$
declare
  c public.rsi_web_challenges%rowtype;
  e public.rsi_passkey_enroll_codes%rowtype;
  kid uuid;
begin
  select * into c from public.rsi_web_challenges where id = p_challenge for update;
  if not found or c.kind <> 'enroll' or c.used_at is not null then
    return 'invalid';
  end if;
  if c.expires_at <= now() then
    return 'expired';
  end if;
  if p_nonce_hash is distinct from c.nonce_hash then
    return 'invalid';
  end if;
  select * into e from public.rsi_passkey_enroll_codes where code_hash = c.enroll_code_hash for update;
  if not found or e.used_at is not null then
    update public.rsi_web_challenges set used_at = now(), result = 'invalid' where id = c.id;
    return 'invalid';
  end if;
  if e.expires_at <= now() then
    return 'expired';
  end if;
  if p_credential_id is null or p_credential_id !~ '^[A-Za-z0-9_-]+$' or length(p_credential_id) not between 16 and 1400
     or p_public_key is null or p_public_key !~ '^[A-Za-z0-9_-]+$' or length(p_public_key) not between 16 and 4000
     or p_sign_count is null or p_sign_count < 0
     or exists (select 1 from public.rsi_passkeys where credential_id = p_credential_id) then
    update public.rsi_web_challenges set used_at = now(), result = 'invalid' where id = c.id;
    return 'invalid';
  end if;
  insert into public.rsi_passkeys (credential_id, public_key, sign_count, transports, device_type, backed_up)
  values (p_credential_id, p_public_key, p_sign_count, coalesce(p_transports, '{}'), p_device_type, p_backed_up)
  returning id into kid;
  update public.rsi_passkey_enroll_codes set used_at = now(), passkey_id = kid where code_hash = e.code_hash;
  update public.rsi_web_challenges set used_at = now(), result = 'enrolled' where id = c.id;
  return 'enrolled';
end $$;

-- ------------------------------------------------------------------------------------------- privileges
-- Supabase grants new objects to anon, authenticated and service_role by name (default privileges). Revoke
-- everything; service_role (the edge function) gets execute on the five web functions and nothing else.
revoke all on table public.rsi_passkeys, public.rsi_passkey_enroll_codes, public.rsi_web_challenges from public;
revoke all on function public.rsi_insert_decision(public.rsi_proposals, text, text, text, text, uuid) from public;
revoke all on function public.amem_rsi_web_challenge(text, uuid, text, text) from public;
revoke all on function public.amem_rsi_web_context(uuid, text) from public;
revoke all on function public.amem_rsi_web_decide(uuid, text, text, bigint) from public;
revoke all on function public.amem_rsi_web_enroll_start(text, text) from public;
revoke all on function public.amem_rsi_web_enroll_finish(uuid, text, text, text, bigint, text[], text, boolean) from public;
do $$
declare r text;
begin
  foreach r in array array['anon', 'authenticated', 'service_role', 'amem_app', 'rsi_reader', 'rsi_writer'] loop
    if exists (select 1 from pg_roles where rolname = r) then
      execute format('revoke all on table public.rsi_passkeys, public.rsi_passkey_enroll_codes, public.rsi_web_challenges from %I', r);
      execute format('revoke all on function public.rsi_insert_decision(public.rsi_proposals, text, text, text, text, uuid) from %I', r);
      execute format('revoke all on function public.amem_rsi_web_challenge(text, uuid, text, text) from %I', r);
      execute format('revoke all on function public.amem_rsi_web_context(uuid, text) from %I', r);
      execute format('revoke all on function public.amem_rsi_web_decide(uuid, text, text, bigint) from %I', r);
      execute format('revoke all on function public.amem_rsi_web_enroll_start(text, text) from %I', r);
      execute format('revoke all on function public.amem_rsi_web_enroll_finish(uuid, text, text, text, bigint, text[], text, boolean) from %I', r);
    end if;
  end loop;
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    grant execute on function public.amem_rsi_web_challenge(text, uuid, text, text) to service_role;
    grant execute on function public.amem_rsi_web_context(uuid, text) to service_role;
    grant execute on function public.amem_rsi_web_decide(uuid, text, text, bigint) to service_role;
    grant execute on function public.amem_rsi_web_enroll_start(text, text) to service_role;
    grant execute on function public.amem_rsi_web_enroll_finish(uuid, text, text, text, bigint, text[], text, boolean) to service_role;
  end if;
end $$;

notify pgrst, 'reload schema';
