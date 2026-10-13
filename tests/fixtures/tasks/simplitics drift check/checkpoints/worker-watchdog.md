### 21:10 — Read spec, contracts, schema v3, transport
Did: read lessons, spec 003, PHASE3_CONTRACTS, watchdog/outbox/tasks stubs, schema v3, retention, surface, alert_transport, qpi_alerts, reporting chat escaping.
Next: implement watchdog.scan + thread, outbox enqueue/drain + thread, surface wiring, tests.
Assumptions: enqueue targets default None = read from env (contract default () kept meaning "none"); stale_qpi = A5 definition (non-terminal latest op, received_at older than 90 s) read from QPI DB in mode=ro; watchdog product '*' for all signals.

### 21:18 — Watchdog + outbox implemented, live sink verified
Did: watchdog.scan (6 checks, DST-safe slots, episodes), tick/run_loop/start_thread; outbox enqueue (P1/P2, D8 owner rule, INSERT OR IGNORE), drain (claim/send/record, lock free during send, backoff, dead-letter), HTTPS sender, Chat text cap; surface start_watchdog/start_outbox + close; 49 new tests; updated the wave-1 stub test in test_hub_tasks_contract.py. Live loopback HTTPS sink on 8110: chat 200, webhook 500 then 200, /dead 500x3 -> dead. Sink stopped. npm test green.
Next: confirm full pytest green, commit, report.
Assumptions: threads start under the same start_retention switch in build_hub_from_env.

### 21:20 — Committed
Did: full pytest 2039 passed / 12 skipped, npm test green; committed 573c189 on worktree-agent-abfec6b5ea4c0ab6d.
Next: none
Status: DONE
