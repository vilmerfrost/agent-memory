### 21:30 — shared label glob in place
Did: added dataops_agent/label_glob.py; site_policy + hub rules use it; docs/config comments; tests/dataops/test_label_glob.py (163 targeted tests pass).
Next: full pytest + npm test, commit.
Assumptions: hub importing a package-root module is fine (it already imports models/severity/webhook).

### 21:35 — full suites running
Did: npm test green (exit 0). Full pytest in progress; one F seen at ~35%, investigating once it finishes.
Next: read the failure, fix or confirm pre-existing, then commit.
Assumptions: none new.

### 21:40 — done
Did: 2 failures were tests using the fnmatch escape "[[]"; changed to plain labels, added migration note to docs/site-policy.md. pytest 2213 passed / 12 skipped; npm test exit 0. Committed 0d75a60.
Next: none
Assumptions: no shipped config uses character classes or "[[]".
Status: DONE
