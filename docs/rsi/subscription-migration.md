# Subscription replay migration

Approved by Vilmer on 2026-10-25. Claude calls use `claude-opus-5-5`; Codex calls use
`gpt-6.1-sol`. Both use **high** effort, including hypothesis writing and judging.
This is implemented behind new profiles. The installed scheduler still selects
`config/rsi-night.first.json`; activation requires dedicated login and live qualification.
The rule-promotion freeze is unchanged.

## Execution boundary

The official, authenticated client runs in a dedicated private home. It has no native
task execution or file tools. It calls the explicit task bridge instead. Each bridge
operation runs in a fresh credential-free Docker worker, without network, Docker socket
or host-home access. Only the task workspace and sanitized arm files are mounted.
Authentication files, tokens and client logs never enter arm snapshots or task mounts.

Claude uses restricted mode, no built-in tools, explicit MCP configuration and disabled
hooks/plugins. It receives the subscription token only in its own environment. The bridge
starts from an empty environment. Codex uses the official app-server protocol pinned to
0.163.0, no environments/executor roots, and source-verified native-tool/extension controls.
Unknown client versions, config conflicts, unavailable models and model changes fail closed.
This pin must be reviewed before upgrading Codex; silently accepting another version is not safe.

Both clients receive high effort explicitly. Codex also verifies the resolved effort in
the thread response. Claude records the explicit client option as its effort evidence;
this is not a claim to observe private reasoning or override organization-enforced caps.

## Profiles and comparison rules

- `config/rsi-night.subscription.json`: routes `.claude` targets to Claude, `.codex` targets
  to Codex, and shared/other targets to both providers.
- `config/rsi-night.subscription-claude.json` and `subscription-codex.json`: bounded
  single-provider qualification profiles.
- A is the current rule and B is the changed rule. Both arms use the same provider,
  exact model, effort, tasks and execution boundary. Fresh task workspaces prevent carryover.
- Shared rules produce separate provider records. A combined passing record requires
  every required provider to improve. Metrics and judge votes are never pooled across models.
- The deterministic implementer stays in place. Jev remains a separate API-backed service.

## Relevance is mandatory

Cases declare exact target paths, kinds and intended behavior. Every changed target must
have a matching case. Before judging, both arms must show successful exposure to those
targets. The subscription bridge records full-file reads and their SHA256 hashes; these
records are constructed by trusted code, not accepted from model output.

Missing target coverage or exposure yields INCONCLUSIVE. Generic regression tasks cannot
establish efficacy; a regression in them still blocks promotion. Old passing records are
superseded before a new provider comparison. Exact-path read evidence proves exposure,
not obedience or semantic correctness of the task design.

The supplied synthetic cases cover `~/.claude/CLAUDE.md`. They can qualify the transport,
but do not establish coverage of arbitrary real skills, Codex-specific files or shared
targets. Those targets remain inconclusive until appropriate case declarations, task
fixtures and objective checks exist. In particular, generic typo/add/TODO tasks do not
qualify a Meltdown change.

## Limits and accounting

The combined profile allows one experiment, up to six provider-specific A/B pairs,
seven Opus calls, twelve Sol calls and seven Jev calls. It keeps the 45-minute wall clock
and the existing $1 API-dollar budget. Subscription usage is excluded from that dollar
subtotal; the observed-token stop is 2,000,000. It is a local resource limit, not a conversion
to provider allowance. Each A/B run reserves eight tool operations. There is no API fallback.

The budget stops subsequent calls when token usage is missing or the observed-token limit
is reached. A running request can consume additional tokens before it ends; this is not
a provider-enforced token ceiling. Client dollar estimates are not counted as subscription
bills. Reports carry subscription tokens, unknown per-run billing and unknown allowance
draw separately from API-reported/estimated costs. Historical reports keep their original values.

Night Desk shows exact setup, task names, judge reasons, target-use gaps and separate
provider results. It labels the dollar subtotal as the API portion when subscriptions are used.

## Authentication and activation

Dedicated homes are under `~/Projects/agent-memory/.subscription-auth/{claude,codex}`.
They must be owner-only. `/.subscription-auth/` is locally excluded through Git's
`info/exclude`, so unrelated `.gitignore` edits are preserved.

- Claude: the native secret popup writes `CLAUDE_CODE_OAUTH_TOKEN` into
  `.subscription-auth/claude/.env`. The token comes from the official `claude setup-token`
  flow. The application loads only that value, with interpolation disabled, and refuses
  symlinks or permissive file ownership/mode. Never paste the token into chat.
- Codex: sign in with the official client using the dedicated `CODEX_HOME` and
  file-backed storage. The client owns refresh. Runs sharing that store are serialized.
  Do not copy the everyday `~/.codex/auth.json` into an experiment or snapshot.

Activation sequence: finish both dedicated sign-ins; verify client status and model
access; run bounded synthetic qualification for each provider; inspect the actual model,
effort, tool boundary, usage and relevance evidence; then change the scheduled config to
the combined profile. A transport smoke test does not authorize rule promotion.
If qualification fails, keep the existing scheduled configuration and report the fixed
error code. Rollback is an explicit config selection, never a silent switch to API billing.

## Validation recorded during implementation

202 backend tests passed in a networkless Docker test runner with read-only source,
empty environment, bounded tmpfs, memory, CPU, processes and wall time. Nine database
tests were skipped because the runner does not expose a database service. An installed
Codex metadata-only check accepted every required isolation configuration override;
no inference was requested. A fresh read-only change review found no actionable defect.
These checks do not prove live subscription access or production readiness.

## Official interface references

- [Claude authentication](https://code.claude.com/docs/en/authentication)
- [Claude CLI options](https://code.claude.com/docs/en/cli-reference)
- [Codex app-server](https://learn.chatgpt.com/docs/app-server)
- [Codex trusted private automation](https://learn.chatgpt.com/docs/auth/ci-cd-auth)
