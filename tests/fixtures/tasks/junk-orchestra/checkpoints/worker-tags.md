### 14:00 — POST validation + tag tests committed
Did: POST /tags now trims, requires non-empty string label (400), maps only errcode 2067 (UNIQUE) to 409 and rethrows others. Rewrote tests/tags.test.js (16 tests, order-independent). Commit a105046.
Next: none
Assumptions: tags-count.test.js left unchanged (already independent of order; worker-stats owns /count). Non-string label -> 400 'label required'.
Status: DONE
