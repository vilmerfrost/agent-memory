# RSI budget reconstruction — night 2026-10-25

## Answer

**Were three experiments incorrectly skipped despite sufficient budget? NO.**

Three *second hypotheses* were deliberately deferred by the one-experiment-per-candidate-per-night policy.
They did not reach experiment creation, did not receive experiment IDs, and were kept pending for later nights.
The old report called them `skipped_budget`, which incorrectly suggested cash exhaustion. The classification is
fixed to `deferred_policy`, with the explicit policy and current budget inputs recorded. Cash remained available.

This does not authorize six experiments. A separate human-set nightly limit is three experiments. Model-call,
container-capacity and time/token limits also apply. None was raised by this change.

## Evidence and currency

- Saved report: `~/sandbox/nights/2026-10-25/report.json`, SHA256
  `888a6675f540eb3d7b6f67ab4ace4f300ce1e8d5a04d2330aed1b0270dc1af1d`.
- Its configuration SHA is `0e4d0646f9cd5403ba13cfb8b8d35694269c6fa9278dd48afca40a825bac1e9e`.
- Its price-table SHA is `9feb9d06389082475b4f79c485f810c2184652260210abec7a1ac92c70ce9d95` (version
  `2026-10-17`), matching the price table inspected for this reconstruction.
- Current subscription config SHA is different. Current source/config are not silently substituted for the saved
  report's historical inputs. The report itself retains the relevant caps; the skip policy is also visible in the
  source at starting commit `7616f84` and pre-night revision `4a90ac17df8dec52dcbf85681a15f63635f41639`.
- `rsi_budget.BudgetConfig.max_usd` and the price table use **USD**, not EUR. The saved cash cap is USD 1.00.
  No historical FX rate or euro invoice was recorded. No conversion or cap change was made.
- The recorded USD 0.11 is a **whole-night, API-only local estimate**. It is not one experiment's measured cost.
  `reported_usd=0`; subscription `billed_usd` and allowance use are NULL. Actual invoiced cost is unknown.

The active installed night configuration is `config/rsi-night.subscription.json`. It uses subscription Opus for
hypotheses/writing/Claude attempts and subscription Sol for Codex attempts and judging. Their usage is constrained
by model-call, token, time and container limits and reported separately; subscription use is not declared free.
Jev is booked at the table's conservative USD 0.01 per call. That rate is explicitly labeled an assumption, not
provider-verified billing. The upgraded subscription model names need no API-token price to book this API-only
cap; accidentally switching those roles to API mode is refused by config validation or the conservative unknown
model pricing path. This is not verification of subscription invoices or actual served model identity.

## Historical ledger

The old budget did not hold cash reservations. It checked an estimate against booked spend before each call,
then booked the provider-reported amount or a local estimate. Therefore cash `reserved` below is **zero**, not
an invented historical reservation. No experiment-level monetary reservation existed.

All five selected/considered candidates were triaged before experiments. Five Jev calls consumed an estimated
USD 0.05. Hypothesizing used subscriptions. Each executed experiment then used two Jev evaluation calls, one
per provider, costing an estimated USD 0.02. Other recorded evidence-provider request charges were zero, not
independently reconciled invoices. Recorded report entry order and the serial orchestrator determine the order.

| Decision / candidate | Planned work at that point | Estimated additional API charge | Cash reserved | Booked API charge before decision | Remaining cash | Recorded actual/provider-reported charge | Decision and exact branch |
|---|---|---:|---:|---:|---:|---|---|
| Triage, five candidates | Five Jev triage calls | USD 0.05 | 0 | 0 | USD 1.00 | Unknown; booked as flat estimates | `JevTriage.assess` → `Budget.call` |
| Pepe hypothesis 1 | One subscription hypothesizer, 12 A/B attempts, six judges, two Jev evaluations | USD 0.02 | 0 | USD 0.05 | USD 0.95 | Unknown | `_investigate`: `take_experiment`, then `_experiment`; ID `exp-20261025-95f15ef6` |
| Pepe hypothesis 2 | No calls: kept pending | 0 charged; prospective complete experiment cost not quoted | 0 | USD 0.07 | USD 0.93 | No invocation | `_investigate` loop `i > 0`: old `skipped_budget`; now `deferred_policy` |
| Meltdown hypothesis 1 | Same provider/pair shape for its own case | USD 0.02 | 0 | USD 0.07 | USD 0.93 | Unknown | `_experiment`; ID `exp-20261025-63a57bf4` |
| Meltdown hypothesis 2 | No calls: kept pending | 0 charged; prospective cost unknown | 0 | USD 0.09 | USD 0.91 | No invocation | Same `i > 0` policy branch |
| Anatomy hypothesis 1 | Same provider/pair shape for its own case | USD 0.02 | 0 | USD 0.09 | USD 0.91 | Unknown | `_experiment`; ID `exp-20261025-93b69041` |
| Anatomy hypothesis 2 | No calls: kept pending | 0 charged; prospective cost unknown | 0 | USD 0.11 | USD 0.89 | No invocation | Same `i > 0` policy branch |

The final USD 0.11 equals five triage calls plus six replay-evaluation calls, eleven Jev calls in total.
Each executed experiment's `cost_usd` is USD 0.02. No historical individual provider invoice was recovered.
The report contains no rejected cash reservation and no unused monetary reservation to release.

## Non-cash accounting

| Resource | Recorded use | Saved cap | Interpretation |
|---|---:|---:|---|
| Executed experiments | 3 | 3 | The independent nightly experiment policy was full |
| Hypotheses | Two accepted per selected candidate | Two per candidate | Second hypotheses were deferred; not six executed experiments |
| Opus calls | 21 | 26 | Three hypothesizers plus 18 Claude agent attempts; no writer calls because of the historical matcher bug |
| Sol calls | 36 | 40 | 18 Codex attempts plus 18 judge calls |
| Jev calls | 11 | 12 | Five triage plus six provider-level evaluation calls |
| Subscription input/output tokens | 1,451,767 / 72,799 | 6,000,000 combined | 1,524,566 recorded total; not cash billing |
| Recorded sandbox check runs | 36 | 360 reserved/run slots | Check containers; subscription attempts are not themselves Docker inference runs |
| Elapsed night | About 1,423 seconds | 17,940 seconds | Cash was not the only possible limiting resource |

A replay uses one case per provider here and three repetitions per provider: six A/B attempts per provider,
12 attempts across Claude/Codex. Each pair gets one judge: six judges per experiment. There is one Jev evaluator
per provider: two paid API calls per experiment. Supporting generation/writer calls, failed calls and retries are
also budgeted when used. The historical writer was never called; the current corrected writer adds up to one
subscription Opus call per implemented intervention. Retries are configured to zero in this saved profile.

Subscription agent adapters conservatively reserve up to eight tool-container slots per attempt before inference.
The code-derived maximum for the 36 attempts is 288 tool slots plus 36 checks = 324 slots. This is a calculated
capacity bound, not a recorded count of 324 actual containers or a cash reservation. Doubling experiments would
also exceed the saved Sol, Jev, experiment and container limits. The dataset does not demonstrate that six full
experiments fit all limits simply because six hypothetical USD 0.11 charges fit the dollar cap.

## Changes and tests

The misleading policy classification was corrected without altering policy. New deferrals record `skip_reason`,
resource (NULL for policy), booked/reserved/remaining USD, experiment and call counts, scope/currency, and price
identity. Actual budget stops name the exhausted resource and its recorded inputs. Briefs distinguish executed
experiments, unstarted budget skips and policy-deferred hypotheses. Historical reports are not rewritten.

A genuine prospective defect was fixed: in-flight estimates were not held, so two concurrent calls could both
pass a cash check and oversubscribe the cap. `Budget` now protects admission/settlement with one lock, holds
estimates until usage is recorded, releases the whole reservation on settlement, and books the completed charge
once. Explicit reservation IDs support out-of-order settlement. Failures and retries have distinct cost records;
unstarted cancellation is explicit, while started calls with missing usage remain conservative unknown-cost
bookings. Each call keeps the price table identity used when it was admitted. Tiny charges are not rounded away
before admission checks. The human-facing existing totals retain their presentation contract.

The existing brief JSON `experiments.total/items` shape is preserved for compatibility. Its total remains the
number of entries; its summary now states policy deferrals and executed experiments separately. The night CLI
also separates `experiments`, `hypothesis_entries` and `policy_deferred`.

Deterministic tests cover six USD 0.11 charge units under USD 1 (without raising the separate experiment cap),
lower actual charges after conservative estimates, concurrent reservations, out-of-order completion, unstarted
cancellation, failures/retries, changing price versions, insufficient remaining funds, missing usage/price,
double settlement, sandbox reservations, nested budget stops and sub-microdollar admission.

**Hard-cap limit:** a reservation prevents local oversubscription of known estimates. It cannot undo an external
provider's unbounded charge above its estimate. Such an overrun is retained, exposed, and stops subsequent work;
the budget does not falsely clamp it to USD 1. Current API-only Jev booking is a fixed conservative local rate,
not invoice proof. A strict provider-billed EUR 1 ceiling cannot be claimed without a provider-enforced bound,
verified billing basis and a defined currency contract. No cap was raised, and no paid experiment was run.

## Measured local overhead

A pure local comparison used starting revision `7616f84` and this implementation, five repetitions of 100
synthetic Jev charge bookings each. Median booking-loop time was 0.4621 ms before and 0.9178 ms after: about
0.4557 ms additional per 100 synthetic calls on this machine. Serialized detailed cost evidence for those 100
calls was 21,987 bytes before and 92,979 bytes after. Both booked the same USD 1.00 of synthetic estimates.
This is a measured local microbenchmark, not production latency, provider spending or physical DB growth.
No added model calls, production database rows or invoice charge occurred during this task.
