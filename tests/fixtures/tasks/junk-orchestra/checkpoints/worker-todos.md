### 12:00 — todos validation, GET /:id, title length limit
Did: Updated src/routes/todos.js (trim+string check, MAX_TITLE_LENGTH=200 -> 'title too long', GET /:id with 400/404, PATCH id check, /count before /:id). Added tests to tests/todos.test.js. npm test: 6 suites passed, 65 tests passed, 1 skipped. Commit f55efba.
Next: none
Assumptions: the scope change was folded into the same single commit.
Status: DONE
