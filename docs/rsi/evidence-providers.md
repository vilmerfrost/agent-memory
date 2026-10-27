# Evidence providers for the nightly investigator (Mission 005.5, Phase C)

`amem rsi night` reads evidence at two stages: **S1** (the delta since the last checkpoint) and **S4** (the
bounded evidence pack for each selected candidate). Both go through the shared evidence interface
(`integrations/evidence.py`: fixed named queries, `run_query`, `EvidenceRef`). Since Mission 005.5 the providers
behind it are the real clients, built by `rsi_evidence_providers.build()` (`rsi_evidence_pack.default_retrievers`).
They are read-only and gated. `amem rsi evidence-probe` runs the same queries once and shows what each provider
returns.

Code: `src/agent_memory/rsi_evidence_providers.py`. Policy: `config/rsi-evidence-providers.json`.
Tests: `tests/test_rsi_evidence_providers.py` (local mock services and a disposable Postgres only).

## The probe

```sh
uv run amem rsi evidence-probe            # one line per provider
uv run amem rsi evidence-probe --json     # the full report (format rsi-evidence-probe/1)
```

Options: `--candidate KEY` (an `rsi_candidates` dedupe key or id, or a rule key; default: the delta's most
frequent deviation rule), `--since ISO` (the delta checkpoint; default: the first-night window of 7 days),
`--policy FILE` (another policy file, for example a copy with past.dev opened for one approved check),
`--timeout-s S` (per-provider time budget; default: the night's 20 s).

It prints, per provider: `status` (ok | error | unavailable; Jev: fixture | live), `reason`, `configured`,
`approved`, `rows` (evidence rows returned), `newest_ts` (the newest data timestamp), `n` (sample sizes: the
first 10 values and their sum), `requests` (HTTP calls, or SQL statements for Supabase), `writes_refused`, each
query with its stage (S1 or S4), and at most three refs. It never prints row contents. Every reason and ref
passes the same neutralizer as the model envelope, so instruction-like text shows as
`[withheld: instruction-like text]`.

It writes nothing: no database row, no night checkpoint, no provider record. It never calls Jev (paid); it only
reports whether Jev is configured and which triage stage the night config uses. Exit code: 0, or 2 for a bad
`--since`.

With nothing configured (verified 2026-10-17):

```text
evidence probe 2026-10-17T20:33:44+00:00 (read-only; policy rsi-evidence-providers.json sha256 df45bfc09d35)
  candidate: -   window (2026-10-10T20:32:44+00:00, 2026-10-17T20:32:44+00:00]
  supabase    unavailable rows=0 newest=- n_sum=0 requests=0
              reason: not configured (AMEM_DB_URL unset)
  clickhouse  unavailable rows=0 newest=- n_sum=0 requests=0
              reason: not configured (AMEM_CLICKHOUSE_URL)
  langfuse    unavailable rows=0 newest=- n_sum=0 requests=0
              reason: not configured (AMEM_LANGFUSE_HOST/_PUBLIC_KEY/_SECRET_KEY)
  pastdev     unavailable rows=0 newest=- n_sum=0 requests=0
              reason: not configured (AMEM_PASTDEV_KEY)
  jev         fixture     configured=no triage=fixture called=no (never called by the probe (paid); ...)
```

Run it from the main checkout, so the app loads `.env.local` itself. Never open the env file to check a key.

## Gates

A provider is asked only when it passes every gate, in this order. The first gate that fails names the reason.
Nothing reaches the network before gate 4.

| # | Gate | Fails as |
|---|---|---|
| 1 | Configured: its env settings are present and valid | `unavailable: not configured (...)` |
| 2 | Switched on: `AMEM_RSI_EVIDENCE_<PROVIDER>=off` is not set; for past.dev also `AMEM_PASTDEV=off` | `unavailable: switched off (...)` |
| 3 | Approved: the policy file says `read: true` and lists the query | `unavailable: not approved: ...` |
| 4 | Health: the provider's own health check (one request) | `unavailable: <detail>` |
| per query | Egress: no parameter names a withheld term; a repo-scoped rule leaves only for a positively own repo | `unavailable: ...` |
| per query | The provider answers | `ok`, or `error` (HTTP error, timeout) |

Gates 2, 3 (the query list) and the egress checks apply to the external providers. For Supabase, the
authoritative store, only `read` in the policy counts; its `queries` entry is informational.

When some of a provider's queries are stopped by policy or egress and the rest answer, the provider's health is
`ok`; the stopped queries stay visible as `unavailable` findings. Only an HTTP error or a timeout makes it `error`.

A sink kill switch (`AMEM_SYNC_<SINK>=off`) stops writes only. It does not stop investigator reads.

`unavailable` and `error` never stop the night. The night's health map keeps its keys and states (supabase,
clickhouse, langfuse, pastdev, jev, sandbox; ok / error / unavailable / fixture).

The policy file is owner-approved. The investigator never writes it. A missing or broken file fails closed: every
external provider is `unavailable: not approved: <problem>`; Supabase stays readable.

## Per provider

| Provider | What is queried | What is approved | Unavailable when |
|---|---|---|---|
| **Supabase** (authoritative) | S1: `delta_since` (new sessions, deviations, eval and routine failures, experiments, evidence rows). S4: `sb.rule_pattern`, `sb.jev_for`, `sb.experiments_for`, `sb.attempts_for`, `sb.decisions_for`, `sb.evidence_for`; re-reads of every `amem:` subject | All rows, read-only (every statement in a READ ONLY transaction). Raw transcripts are never read here. | Probe: `AMEM_DB_URL` unset. Both: the connection fails. |
| **ClickHouse Cloud** | S1: `rule_gap`, `routine_streaks` over the window's days. S4: `rule_gap`, `rule_weekly_rate` for the candidate's rule, `routine_streaks` for a routine rule | Read-only retrieval (Mission 005.5 brief, section 5). The data is what the approved sink wrote: numbers, labels and pseudonyms only. Today that is only the C1 synthetic rows. Their `amem:` subjects do not resolve in production, so their findings show as `stale`, never as authority. No historical backfill is approved. | `AMEM_CLICKHOUSE_URL` unset or bad; switched off; the rule names a withheld term; the rule is repo-scoped and its repo is not positively own; a repo-scoped rule without `AMEM_PSEUDONYM_KEY` (then `error`) |
| **Langfuse Cloud EU** | S1: `experiment_scores` for at most 2 experiments in the window. S4: `experiment_scores` per earlier experiment (at most 2), `trace` for at most 2 failing stored traces | Read-only retrieval. Sandbox experiment traces and scores only: ids, numbers, labels. | Host or keys unset; switched off; an experiment id names a withheld term |
| **past.dev** | S4: `recall` with the candidate rule id's words (at most 6 results), each recalled `amem:` ref re-read in Supabase | **Approved for investigator recall on 2026-10-24** (policy `read: true`; Vilmer: "everything should be turned on"). This replaces the earlier exclusion of "recall from routines" (`docs/integrations/m004c/backfill-export-plan.md` 5.8). The data is the sanitized own decision records the sink sent. Cost: 0.1 credit per recall, one recall per evidence pack, at most `max_experiments` packs a night (hard cap 3): at most 0.3 credit a night, 1.2 if past.dev billed each of the client's 3 retries after a 5xx or a rate-limit 429 (not verified). The probe adds one recall per manual run. | Key unset; `AMEM_PASTDEV=off` or `AMEM_RSI_EVIDENCE_PASTDEV=off`; a repo-scoped rule of a repo that is not positively own; a withheld term in the words |
| **Jev** | Not by the probe. The night's S3 triage and S7 replay use Jev in the mode the night config sets | Fixture mode by default (no call, no cost) | The probe reports `fixture`, or `live` when the key, `AMEM_JEV_LIVE=1` and a live triage stage are all set; it still never calls |

### Repository ownership and the egress deny list

A query parameter that leaves the Mac is checked first:

- It must not contain a withheld term: `AMEM_EGRESS_DENY`, plus every team repo and project name, and every private
  one that no own session uses (`egress.team_names`, read from Supabase, read-only). It must not look like an
  email. Pure numbers (days, limits) are not checked.
- A repo-scoped rule (`claude-md:<repo>:...`, `agents-md:<repo>:...`) leaves only when an own session has that repo
  or project and no team session does (the egress F8 rule). For ClickHouse that is the `rule` parameter; the
  repo part is then sent as its keyed pseudonym. For past.dev it is the candidate's rule, because the recall words
  come from it.

Most hosted sessions are `private` today: rows ingested before Mission 004C have no `meta.remote_host`. So a
repo-scoped rule is `unavailable` for ClickHouse and past.dev until `amem sessions hosts --apply` runs (a hosted
write that needs its own approval). Global, skill, agent and routine rules are not affected.

## Read-only by construction

| Provider | What the client can send | Everything else |
|---|---|---|
| Supabase | Statements in READ ONLY transactions (autocommit off, `read_only` on; no session-level SET, which a transaction pooler would leak to other clients) | A write fails with `ReadOnlySqlTransaction` |
| ClickHouse | `GET /ping`, and `POST /` with exactly one of the fixed named SELECTs in `clickhouse.QUERIES` | `insert`, `command`, any other statement: `ReadOnlyViolation` before a request |
| Langfuse | GET only | span export, scores, datasets, dataset items, any POST: `ReadOnlyViolation` |
| past.dev | `GET /ingest/<nil uuid>` (free health) and `POST /recall` | ingest, retry, delete, estimate, usage: `ReadOnlyViolation` |

There is no sync, no export, no recurring job and no transcript text in any request.

## Untrusted data

Provider answers become `EvidenceRef`s: refs parsed by the ref grammar, metrics with numbers and labels only, text
only from Supabase rows (scrubbed). A model sees evidence only through `render_for_model`: a fixed preface, a
nonce-delimited block, every string neutralized. Every finding keeps its source ref, its timestamp and its sample
size `n` where the source has one. For ClickHouse the finding time is the query time; the probe's `newest_ts` uses
the data's own `week_start`.

## Known limits

- **ClickHouse cold start.** An idle Cloud service wakes on the first request (about 15-17 s in C1). The night
  gives each provider 20 s per stage, so a cold first query can time out (`error: timeout`). The next stage starts
  warm.
- **Langfuse Hobby rate limit.** Calls remain spaced 2.1 s apart. Dataset-less `experiment_scores` now uses
  one paged score query over at most 25 exact root trace IDs, rather than one score query per trace.
  Throttling and pages share the caller's remaining read budget; a read request is not retried inside it.
  Unread/repeated cursors fail as `IncompleteRead`; expired deadlines fail as `ReadDeadline`. The outer 20 s
  limit is unchanged. A selected trace cap is explicit as `truncated=true`, `population_complete=false`.
  Missing check scores remain unknown, not a zero pass rate. See [Langfuse read budget](langfuse-read-budget.md).
- **S1 reads Supabase only in the night today.** `delta_since` accepts providers, but `rsi_night.PackEvidence.delta`
  does not pass them yet (that file belongs to the night-ops worker). The probe already runs the S1 provider
  queries, so it shows what the night would get once that edit lands.
- **ClickHouse holds only synthetic rows.** Until a reviewed historical backfill is approved and run, ClickHouse
  findings in production are `stale` (their subjects do not resolve) and add context only.
- **Not verified against the hosted services from the worker.** The tests use local mocks with the documented
  request shapes. The parent runs the probe live after the merge.

## Opening past.dev for the investigator (owner decision)

1. Decide that the night may recall from past.dev (0.1 credit per candidate, at most one recall per pack).
2. Set `providers.pastdev.read` to `true` in `config/rsi-evidence-providers.json` and write the approval reference
   in `approval`. Commit it.
3. Make sure `AMEM_PASTDEV` is not `off` for the night's process.
4. Check with `uv run amem rsi evidence-probe`: past.dev shows `ok`, one recall request, only `past:amem:` refs.

To try it once without changing the policy: copy the file, open past.dev in the copy, and pass
`--policy <copy>` to the probe. The night always reads the committed file.
