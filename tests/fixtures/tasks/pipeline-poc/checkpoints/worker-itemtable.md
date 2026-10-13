### 16:31 — Research done
Did: Read itemTable(), DatasheetSheet.vue markup, Purmo data-sheets.js/data-sheet.html, both PDFs. Purmo sorts types with default .sort() and drops rows without a type; renders volume/n/weight as parseFloat(x || 0).toFixed(n), so missing and 0 both show 0.0. Our 1022 rows: volume "0.0" means datablad sends a real 0 (fixed() returns "" for absent/null/""); weight empty means absent/null.
Next: tests first (type groups, missing vs zero), then implement types[] on ItemTableGroup, keep rows flat for DatasheetsView/export.
Assumptions: backend cannot run on host and datablad is a client live system (STOP-2), so field shape is inferred from code + our PDF, not a live call.

### 16:33 — Implemented, tested, pushed
Did: itemTable() returns types[] per height (types numeric asc, rows by length, untyped last) plus flat rows in that order; missing volume/n/weight -> "–", real 0 -> 0.0. DatasheetSheet.vue: one tbody per type with tr.type-row bar, type cell empty. New tests in datasheets.test.ts and DatasheetSheet.test.ts, failing first. Decision 0005 updated. vitest 343/343, vue-tsc clean, prettier clean. Commit 10ef3d3 pushed.
Next: none
Status: DONE
Assumptions: as above.
