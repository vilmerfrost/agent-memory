-- RSI V1 approval-first core (Mission 003; docs/rsi/spec.md, docs/rsi/approval.md).
-- Experiments, durable candidates, shared evidence, immutable artifacts, bound evaluation results,
-- proposals, phone/CLI decisions (append-only), the promotion worker's results, job checkpoints,
-- and the session environment used to keep sandbox sessions out of production evaluations.
-- Nothing here changes production config: promotion is agent-config's `improve promote`, run by the
-- promotion worker (`amem rsi decide-poll`) after a decision row exists.

-- sessions.env: prod | eval | sandbox | cloud (null = not classified yet, treated as prod).
alter table public.sessions add column if not exists env text
    check (env is null or env in ('prod', 'eval', 'sandbox', 'cloud'));
alter table public.sessions add column if not exists experiment_id text;
create index if not exists sessions_env_idx on public.sessions (env);

-- edits: a staged edit (AMEM_EDITS_MODE=stage) is a row too; its proposal is in rsi_proposals.
alter table public.edits drop constraint if exists edits_status_check;
alter table public.edits add constraint edits_status_check check (status in ('proposed', 'kept', 'trial', 'auto-reverted',
    'refused', 'reverted', 'suggested', 'skipped', 'staged'));

create table if not exists public.rsi_experiments (
    id          text primary key check (id ~ '^[a-z0-9][a-z0-9-]{2,60}$'),
    night       text,
    kind        text not null,
    hypothesis  jsonb not null default '{}'::jsonb,
    baseline_id text,
    status      text not null default 'planned'
                check (status in ('planned', 'running', 'evaluated', 'failed', 'aborted')),
    verdict     text,
    meta        jsonb not null default '{}'::jsonb,
    created_at  timestamptz not null default now(),
    finished_at timestamptz
);

-- Durable candidates: not selected tonight = still here tomorrow. The loop never deletes them; it
-- moves `status` and refreshes last_seen_at / seen_count on every sighting (upsert by dedupe_key).
create table if not exists public.rsi_candidates (
    id                 uuid primary key default gen_random_uuid(),
    dedupe_key         text not null unique,
    source             text not null,
    kind               text not null,
    target             text,
    rule_key           text,
    evidence_refs      text[] not null default '{}',
    metrics            jsonb not null default '{}'::jsonb,
    jev                jsonb,
    rank               real,
    status             text not null default 'new'
                       check (status in ('new', 'deferred', 'selected', 'tested', 'staged', 'rejected', 'retired')),
    decided_reason     text,
    seen_count         int not null default 1,
    first_seen_at      timestamptz not null default now(),
    last_seen_at       timestamptz not null default now(),
    last_considered_at timestamptz,
    meta               jsonb not null default '{}'::jsonb
);
create index if not exists rsi_candidates_open_idx on public.rsi_candidates (status, rank desc)
    where status in ('new', 'deferred');

-- Shared structured findings from any system (ClickHouse, Langfuse, past.dev, NCM, Jev ...). The
-- investigator cites these by id instead of carrying them as prompt text. Summaries are sanitized
-- and short; the authoritative records stay where they are (subject_refs, external_ref).
create table if not exists public.rsi_evidence (
    id           uuid primary key default gen_random_uuid(),
    system       text not null check (system in ('supabase', 'clickhouse', 'langfuse', 'pastdev', 'ncm',
                                                 'jev', 'nightly-trials', 'evals', 'manual')),
    kind         text not null,
    subject_refs text[] not null default '{}',
    summary      text check (summary is null or length(summary) <= 2000),
    metrics      jsonb not null default '{}'::jsonb,
    external_ref text,
    observed_at  timestamptz not null default now(),
    valid_until  timestamptz,
    created_at   timestamptz not null default now(),
    unique (system, external_ref)
);

-- Artifacts: the exact tested bytes. hash = sha256(manifest_canonical), checked by the database.
-- files: {after_sha256: text}. Immutable (trigger below).
create table if not exists public.rsi_artifacts (
    hash               text primary key check (hash ~ '^[0-9a-f]{64}$'),
    manifest_canonical text not null,
    manifest           jsonb not null check (manifest ->> 'format' = 'rsi-artifact/1'),
    files              jsonb not null,
    experiment_id      text,
    baseline_id        text not null,
    created_at         timestamptz not null default now(),
    check (encode(sha256(convert_to(manifest_canonical, 'UTF8')), 'hex') = hash)
);

create table if not exists public.rsi_eval_results (
    id            uuid primary key default gen_random_uuid(),
    artifact_hash text not null references public.rsi_artifacts (hash),
    experiment_id text,
    baseline_id   text not null,
    suite         text not null,
    suite_version text not null,
    model         text,
    effort        text,
    verdict       text not null check (verdict in ('pass', 'improves', 'no-effect', 'worse', 'fail', 'inconclusive')),
    record        jsonb not null check (record ->> 'format' = 'rsi-eval/1'),
    record_sha    text not null check (record_sha ~ '^[0-9a-f]{64}$'),
    created_at    timestamptz not null default now(),
    unique (artifact_hash, suite, record_sha)
);

create table if not exists public.rsi_proposals (
    id            uuid primary key default gen_random_uuid(),
    artifact_hash text not null references public.rsi_artifacts (hash),
    experiment_id text,
    candidate_id  uuid references public.rsi_candidates (id),
    title         text not null,
    summary       text,
    rationale     jsonb not null default '{}'::jsonb,
    evidence_refs text[] not null default '{}',
    risk          jsonb,
    confidence    real,
    expected      jsonb,
    targets       text[] not null,
    status        text not null default 'staged'
                  check (status in ('staged', 'approved', 'rejected', 'expired', 'stale', 'promoting',
                                    'promoted', 'failed', 'reverted')),
    change_id     text,
    result        text,
    meta          jsonb not null default '{}'::jsonb,
    created_at    timestamptz not null default now(),
    expires_at    timestamptz not null default now() + interval '72 hours',
    decided_at    timestamptz,
    promoted_at   timestamptz,
    reverted_at   timestamptz
);
create unique index if not exists rsi_proposals_live_artifact_idx on public.rsi_proposals (artifact_hash)
    where status in ('staged', 'approved', 'promoting', 'promoted');

-- One random token per (proposal, action); only sha256(token:proposal:artifact_hash:action) is stored.
create table if not exists public.rsi_decision_tokens (
    proposal_id uuid not null references public.rsi_proposals (id) on delete cascade,
    action      text not null check (action in ('apply', 'reject', 'undo')),
    token_hash  text not null check (token_hash ~ '^[0-9a-f]{64}$'),
    expires_at  timestamptz not null,
    used_at     timestamptz,
    primary key (proposal_id, action)
);

-- Decisions: append-only audit history (trigger below). One per proposal and action.
create table if not exists public.rsi_decisions (
    id            uuid primary key default gen_random_uuid(),
    proposal_id   uuid not null references public.rsi_proposals (id),
    action        text not null check (action in ('apply', 'reject', 'undo')),
    artifact_hash text not null check (artifact_hash ~ '^[0-9a-f]{64}$'),
    source        text not null check (source in ('phone', 'cli')),
    verified      text not null check (verified in ('edge-token', 'cli-tty')),
    reason        text,
    decided_at    timestamptz not null default now(),
    expires_at    timestamptz not null,
    unique (proposal_id, action)
);

-- What the promotion worker did with a decision (kept apart so decisions stay immutable).
create table if not exists public.rsi_decision_results (
    decision_id uuid primary key references public.rsi_decisions (id),
    result      text not null,
    change_id   text,
    detail      text,
    attempts    int not null default 0,
    handled_at  timestamptz
);

-- Dependency-aware scheduling: a job records completion per run key (usually the night); downstream
-- jobs check their upstream's row instead of trusting the clock.
create table if not exists public.rsi_checkpoints (
    job           text not null,
    run_key       text not null,
    status        text not null check (status in ('running', 'done', 'failed', 'skipped')),
    started_at    timestamptz not null default now(),
    finished_at   timestamptz,
    last_event_id bigint,
    meta          jsonb not null default '{}'::jsonb,
    primary key (job, run_key)
);

-- Immutability: artifacts, evaluation results and decisions are never updated or deleted.
create or replace function public.rsi_immutable() returns trigger
language plpgsql set search_path = public, pg_temp as $$
begin
  raise exception '% is append-only (RSI V1 audit trail)', tg_table_name;
end $$;

drop trigger if exists rsi_artifacts_immutable on public.rsi_artifacts;
create trigger rsi_artifacts_immutable before update or delete on public.rsi_artifacts
    for each row execute function public.rsi_immutable();
drop trigger if exists rsi_eval_results_immutable on public.rsi_eval_results;
create trigger rsi_eval_results_immutable before update or delete on public.rsi_eval_results
    for each row execute function public.rsi_immutable();
drop trigger if exists rsi_decisions_immutable on public.rsi_decisions;
create trigger rsi_decisions_immutable before update or delete on public.rsi_decisions
    for each row execute function public.rsi_immutable();
-- Row triggers do not fire on TRUNCATE; guard it too (cascades from rsi_proposals included).
drop trigger if exists rsi_artifacts_no_truncate on public.rsi_artifacts;
create trigger rsi_artifacts_no_truncate before truncate on public.rsi_artifacts
    for each statement execute function public.rsi_immutable();
drop trigger if exists rsi_eval_results_no_truncate on public.rsi_eval_results;
create trigger rsi_eval_results_no_truncate before truncate on public.rsi_eval_results
    for each statement execute function public.rsi_immutable();
drop trigger if exists rsi_decisions_no_truncate on public.rsi_decisions;
create trigger rsi_decisions_no_truncate before truncate on public.rsi_decisions
    for each statement execute function public.rsi_immutable();

-- Phone decision (edge function amem-decide, service role). Records a decision only; never promotes.
-- Returns: recorded | already | denied | expired | missing | hash-mismatch | not-staged | not-promoted | invalid
create or replace function public.amem_rsi_record_decision(p_proposal uuid, p_action text, p_artifact_hash text,
                                                           p_token text) returns text
language plpgsql security definer set search_path = public, pg_temp as $$
declare
  pr public.rsi_proposals%rowtype;
  tk record;
  th text;
begin
  if p_action not in ('apply', 'reject', 'undo') or p_token is null or length(p_token) < 16 then
    return 'invalid';
  end if;
  select * into pr from public.rsi_proposals where id = p_proposal for update;
  if not found then
    return 'missing';
  end if;
  if pr.artifact_hash is distinct from p_artifact_hash then
    return 'hash-mismatch';
  end if;
  th := encode(sha256(convert_to(p_token || ':' || p_proposal::text || ':' || p_artifact_hash || ':' || p_action, 'UTF8')), 'hex');
  select * into tk from public.rsi_decision_tokens where proposal_id = p_proposal and action = p_action for update;
  if not found or tk.token_hash <> th then
    return 'denied';
  end if;
  if tk.used_at is not null then
    return 'already';
  end if;
  if tk.expires_at <= now() then
    return 'expired';
  end if;
  return public.rsi_insert_decision(pr, p_action, 'phone', 'edge-token', null);
end $$;

-- Vilmer's terminal (`amem rsi decide`, TTY-only, refused in agent sessions). No token: the CLI is the
-- check. Same state rules as the phone path.
create or replace function public.amem_rsi_record_cli_decision(p_proposal uuid, p_action text, p_artifact_hash text,
                                                               p_reason text) returns text
language plpgsql security definer set search_path = public, pg_temp as $$
declare
  pr public.rsi_proposals%rowtype;
begin
  if p_action not in ('apply', 'reject', 'undo') then
    return 'invalid';
  end if;
  select * into pr from public.rsi_proposals where id = p_proposal for update;
  if not found then
    return 'missing';
  end if;
  if pr.artifact_hash is distinct from p_artifact_hash then
    return 'hash-mismatch';
  end if;
  if exists (select 1 from public.rsi_decisions where proposal_id = p_proposal and action = p_action) then
    return 'already';
  end if;
  return public.rsi_insert_decision(pr, p_action, 'cli', 'cli-tty', p_reason);
end $$;

create or replace function public.rsi_insert_decision(pr public.rsi_proposals, p_action text, p_source text,
                                                     p_verified text, p_reason text) returns text
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
  insert into public.rsi_decisions (proposal_id, action, artifact_hash, source, verified, reason, expires_at)
  values (pr.id, p_action, pr.artifact_hash, p_source, p_verified, p_reason, now() + interval '24 hours');
  update public.rsi_proposals
     set status = case p_action when 'apply' then 'approved' when 'reject' then 'rejected' else status end,
         decided_at = coalesce(decided_at, now())
   where id = pr.id;
  return 'recorded';
end $$;

-- Least-privilege roles for automation (NOLOGIN here; activation gives them a login + password).
do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'rsi_reader') then
    create role rsi_reader nologin;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'rsi_writer') then
    create role rsi_writer nologin;
  end if;
end $$;

grant usage on schema public to rsi_reader, rsi_writer;
grant select on public.sessions, public.runs, public.scores, public.deviations, public.edits, public.skill_calls,
    public.skill_scores, public.handoffs, public.rsi_experiments, public.rsi_candidates, public.rsi_evidence,
    public.rsi_artifacts, public.rsi_eval_results, public.rsi_proposals, public.rsi_decisions,
    public.rsi_decision_results, public.rsi_checkpoints to rsi_reader, rsi_writer;
grant insert, update on public.rsi_experiments, public.rsi_candidates, public.rsi_evidence, public.rsi_checkpoints,
    public.rsi_decision_results to rsi_writer;
grant insert on public.rsi_artifacts, public.rsi_eval_results, public.rsi_decision_tokens to rsi_writer;
grant insert, update (status, change_id, result, meta, promoted_at, reverted_at) on public.rsi_proposals to rsi_writer;
-- rsi_writer gets NO insert on rsi_decisions and NO execute on the decision functions: only the edge
-- function (service role) and the owner's TTY-guarded CLI record decisions.
revoke all on function public.amem_rsi_record_decision(uuid, text, text, text) from public;
revoke all on function public.amem_rsi_record_cli_decision(uuid, text, text, text) from public;
revoke all on function public.rsi_insert_decision(public.rsi_proposals, text, text, text, text) from public;

-- RLS on everywhere; read policies for the two automation roles; the Supabase API roles get nothing.
do $$
declare t text;
begin
  foreach t in array array['rsi_experiments', 'rsi_candidates', 'rsi_evidence', 'rsi_artifacts', 'rsi_eval_results',
                           'rsi_proposals', 'rsi_decision_tokens', 'rsi_decisions', 'rsi_decision_results',
                           'rsi_checkpoints'] loop
    execute format('alter table public.%I enable row level security', t);
    if t <> 'rsi_decision_tokens' then
      execute format('drop policy if exists rsi_read on public.%I', t);
      execute format('create policy rsi_read on public.%I for select to rsi_reader, rsi_writer using (true)', t);
    end if;
  end loop;
  foreach t in array array['rsi_experiments', 'rsi_candidates', 'rsi_evidence', 'rsi_checkpoints',
                           'rsi_decision_results', 'rsi_artifacts', 'rsi_eval_results', 'rsi_proposals',
                           'rsi_decision_tokens'] loop
    execute format('drop policy if exists rsi_write_ins on public.%I', t);
    execute format('create policy rsi_write_ins on public.%I for insert to rsi_writer with check (true)', t);
  end loop;
  foreach t in array array['rsi_experiments', 'rsi_candidates', 'rsi_evidence', 'rsi_checkpoints',
                           'rsi_decision_results', 'rsi_proposals'] loop
    execute format('drop policy if exists rsi_write_upd on public.%I', t);
    execute format('create policy rsi_write_upd on public.%I for update to rsi_writer using (true) with check (true)', t);
  end loop;
  foreach t in array array['sessions', 'runs', 'scores', 'deviations', 'edits', 'skill_calls', 'skill_scores', 'handoffs'] loop
    execute format('drop policy if exists rsi_read on public.%I', t);
    execute format('create policy rsi_read on public.%I for select to rsi_reader, rsi_writer using (true)', t);
  end loop;
end $$;

do $$
declare r text; t text;
begin
  foreach r in array array['anon', 'authenticated'] loop
    if exists (select 1 from pg_roles where rolname = r) then
      foreach t in array array['rsi_experiments', 'rsi_candidates', 'rsi_evidence', 'rsi_artifacts', 'rsi_eval_results',
                               'rsi_proposals', 'rsi_decision_tokens', 'rsi_decisions', 'rsi_decision_results',
                               'rsi_checkpoints'] loop
        execute format('revoke all on table public.%I from %I', t, r);
      end loop;
      execute format('revoke all on function public.amem_rsi_record_decision(uuid, text, text, text) from %I', r);
      execute format('revoke all on function public.amem_rsi_record_cli_decision(uuid, text, text, text) from %I', r);
    end if;
  end loop;
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    grant execute on function public.amem_rsi_record_decision(uuid, text, text, text) to service_role;
  end if;
end $$;

notify pgrst, 'reload schema';
