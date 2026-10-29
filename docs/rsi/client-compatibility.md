# Subscription client readiness

CLI versions are recorded as diagnostic evidence. They are not an allowlist. Codex
must satisfy the effective runtime configuration, no-host permission profile,
subscription account, exact model/effort and accepted thread permission contract.
The native execution exclusion switch remains an emergency operator stop, not a
compatibility receipt. Native tool events are rejected at item start and completion.

The real subscription replay path checks readiness in S0, before targets, evidence
analysis or experiments. All configured unique provider/model/effort/auth-home/tool
roles pass metadata checks first. Then bounded real probes verify responses, usage,
and (for agent roles) a nonce written through the isolated task tool. These probes
use the existing call/token budgets. Root qualification additionally checks denied
host reads/writes; a successful model reply alone is not isolation proof.

Failures proven to precede inference cancel the monetary reservation while retaining
the attempt slot. The original compatibility or authentication reason survives.
After inference may have started, missing usage remains unknown and stops further
work. Briefs describe this as unavailable usage accounting, not exhausted money.

A late archive may satisfy a missed night's prerequisite only if its latest result
is PASS, newer than that night and no older than 26 hours. A newer failed archive
still blocks. Other prerequisite checks and nightly limits remain unchanged.

## Midnight maintenance

`amem client-maintenance` runs `codex update` and `claude install latest` with
bounded process groups and no application secrets. It resolves the client paths
again afterward. An updater/download failure is a warning when the remaining
installed clients pass readiness. Readiness failure names its provider/role and
blocks experiments. No automatic API-billing fallback or token rotation occurs.

The maintenance job takes an exclusive filesystem lock. Daily and night jobs take
shared locks for their whole run, including late recovery. A busy pipeline makes
maintenance skip; no active job is killed. Catch-up after 00:20 skips updating,
keeping the 00:40 daily slot clear. The midnight job checks all night roles plus
one bounded Sonnet request through the actual daily writer route. Quiet successful
runs write a report without sending a notification. Failure or update warnings
send an actionable notification. Register `amem-client-maintenance` with a 25-hour
freshness limit and install the supplied 00:00 LaunchAgent only after deployment.

## Qualification on 29 October 2026

Root ran all four roles on Codex 0.165.0 and Claude with exact requested models,
correct responses/tool-written files and recorded usage. Receipt:
`~/.claude/parent/agent-memory/reports/client-qualification-0.165.json`.
A separate real Codex host-access denial canary used the isolated Docker tool.
The external write marker remained absent, and the private random read sentinel
was not disclosed. The client reported FileNotFoundError for both paths. This
proves the observed write boundary; the read evidence is limited to the undisclosed
sentinel and recorded isolated-tool use. Receipt:
`~/.claude/parent/agent-memory/reports/codex-0.165-host-denial.json`.
