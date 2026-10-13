### 21:55 — Read brief and code, chose design
Did: read lessons, threat model, watchdog/outbox/surface/schema, tests. Proved in a scratch DB that a table rebuild under FK=ON inside the migration txn fails; SQLite's documented writable_schema CHECK-loosening works (commit + rollback).
Next: v4 migration (add notification_dead to WATCHDOG_SIGNALS via writable_schema), liveness heartbeats + /v1/hub/health/workers + /healthz 503, notification_dead check in watchdog scan (derived from outbox sent/dead per installation+sink), first-scan unreadable QPI fallback, redirect_denied test with local TLS sink.
Assumptions: dashboard/server.py /healthz needs a 2-line hook (not in owned list, unavoidable); plain Docker marks unhealthy but does not restart by itself — documented.

### 22:01 — liveness + v4 + watchdog checks written
Did: schema v4 committed (tests green). Added hub/liveness.py heartbeats, run_loop heartbeat hooks, surface worker_health/healthy/healthz + GET /v1/hub/health/workers, server.py /healthz hook; notification_dead check and QPI first-scan fallback in watchdog.scan. Liveness tests pass (8).
Next: tests for notification_dead (no loop) and QPI first scan, redirect_denied TLS test, docs, full suite, live check.
Assumptions: notification_dead is per installation (task installation), derived from outbox sent/dead rows per sink+target.

### 22:03 — features + tests committed
Did: all four items implemented and tested; pytest 2249 passed / 12 skipped, npm test green; docs + threat model updated; committed.
Next: live check on PORT 8110 (hub dev, var/ DB, local failing HTTPS sink -> dead letter -> notification_dead task), then report.
Assumptions: live check uses a short watchdog/outbox interval only via a scratch launcher script, not product env.

### 22:06 — live check done, final
Did: live check on 8110 (real CLI: /healthz 200, health/workers 200, POST 405; harness w/ short intervals: legacy FAIL report -> P2 task -> 8x500 -> dead -> notification_dead task; its own alert died, no 3rd task, 16 sink hits total; killed watchdog -> /healthz 503). All stopped. pytest 2249 passed/12 skipped, npm test green. HEAD 6289321.
Next: none
Status: DONE
