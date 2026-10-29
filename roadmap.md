# Roadmap

Read this file when Vilmer asks what remains, what to improve, or what to work on next.
These entries are deferred ideas. Listing them does not authorize implementation,
new provider calls, broader data access, or a release.

## Extra ClickHouse analytics for the nightly investigator

Status: deferred by Vilmer on 29 October 2026. Keep these separate from the current
data-use repairs. The night currently selects rule-gap, weekly rule-rate and
routine-streak queries. The additional queries already exist, but the nightly
provider policy and evidence selection do not select them.

| Priority | Question | Existing query | What needs deciding |
|---|---|---|---|
| 1 | Which model and effort settings use more tokens for a recorded outcome? | `tokens_by_outcome` | Choose a useful comparison, minimum sample size and evidence limits. Outcome labels are observations, not proof of better quality or necessary effort. Tokens do not establish subscription cost. |
| 2 | Which skills are associated with completed work and token use? | `skill_outcomes` | Separate skill use from task difficulty and model choice. An association is not proof the skill caused the outcome. Keep existing Supabase skill analysis distinct. |
| 3 | Are external research runs useful to a current investigation? | `research_runs` | Add only when real relevant research data and an explicit nightly question exist. Do not create research/model runs just to populate the dashboard. |

Before enabling an entry, define the question and population, inspect coverage and
freshness, and agree the bounded query/evidence policy. Preserve privacy checks,
existing budgets, and source-of-truth checks. Record returned, excluded and used
results separately. A passing connection test is not proof the night used the data.

## Current boundaries

- Supabase remains the source of truth; ClickHouse is an analytics copy.
- Reading existing lesson-decision history does not unfreeze v1 lesson generation,
  distillation or instruction injection.
- Pipeline and Purmo application changes stay local and out of agent-system releases.
- The next natural scheduled night is separate acceptance evidence from local tests
  or manual connection checks.

Related code: [provider policy](config/rsi-evidence-providers.json),
[nightly evidence selection](src/agent_memory/rsi_evidence_pack.py), and
[ClickHouse query definitions](src/agent_memory/integrations/clickhouse.py).
