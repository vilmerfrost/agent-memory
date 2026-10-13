### 21:57 — code + tests for all 4 items
Did: bulk dismiss refuses P1 (409 + p1TaskIds); dry run on read-only WAL snapshot off write_lock with rules-generation recheck + recompute (last attempt under lock); exact signal cap via _Tally.full(); CLI rules actor cli:<osuser>[ as <actor>], empty --actor refused. New tests pass (rules/tasks API).
Next: full pytest + npm test, threat model update, live check, commit.
Assumptions: no schema change needed; _db_file PRAGMA database_list is lock-free (serialized sqlite).

### 21:59 — verified + committed
Did: pytest 2233 passed / 12 skipped; npm test green; live check on :8100 (bulk dismiss w/ P1 409, single dismiss 200, rule create 201, dry-run 200; server stopped; CLI create -> admin_audit actor cli:vilmerfrost, --actor anna -> "cli:vilmerfrost as anna", blank actor exit 3). Threat model items 13 closed, 9 partly closed. Commit e7f3a94.
Next: none
Status: DONE
