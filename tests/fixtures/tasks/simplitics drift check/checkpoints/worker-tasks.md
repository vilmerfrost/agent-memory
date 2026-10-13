### 21:09 — Read spec and contracts
Did: read lessons, spec (Tasks, D1-D10, invariants, scenarios), PHASE3_CONTRACTS, tasks/rules/outbox/schema/store/installations_api/operator_auth/surface.
Next: implement tasks.apply_signals engine, ingest hook in store.py, tasks_api.py + surface block, tests.
Assumptions: apply_signals calls outbox.enqueue itself (contract call graph); absent-finding recovery via an internal tasks helper since error_group keys cannot be rebuilt from a task row; snoozed wake also swept at the start of apply_signals so the watchdog tick wakes expired snoozes.

### 21:16 — Engine, hook, API written
Did: tasks.apply_signals/apply_run/wake_due engine; new hub/classify.py (rule eval, classifications, grouping, control signals); SAVEPOINT task_engine hook in store.ingest with classification_failed counter; new hub/tasks_api.py + surface block. Existing hub tests green.
Next: write tests for scenarios 11-15, 17-20 and invariants; live API check; full suite.
Assumptions: findings of an EXPECTED control (control-scope policy) have base EXPECTED; bulk body uses text field; truncated alarming control with listed findings = WARN control task.

### 21:24 — Done
Did: merged integration (rules + watchdog), switched hook to rules.load_rules/control_result, outbox.enqueue now gets every event; 39 new tests; full suite 2127 passed 12 skipped; npm test green; live API check on 8120 all expected codes; server stopped.
Next: none
Assumptions: watchdog tick only calls apply_signals when it has signals, so expired snoozes wake on the next signal (open item for parent).
Status: DONE
