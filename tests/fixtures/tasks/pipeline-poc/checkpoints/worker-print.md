### 15:42 — Read code, chose design
Did: read lessons, renamed branch to feat/datasheets-print-preview, read DatasheetEditView/DatasheetSheet/EditableText/DatasheetSheetView.
Next: implement preview as a fixed overlay with a second, plain (non-editable) DatasheetSheet; editor stays mounted and inert underneath so scroll, lock and drafts are kept.
Assumptions: an open sheet edit is committed (saved) on entering preview, like any other focus move.

### 15:44 — Implemented, tested, pushed
Did: print preview overlay + plain sheet prop + 2 tests; root vitest 328 pass, func-node 460 pass, vue-tsc clean, prettier clean. Committed a0cb72d, pushed feat/datasheets-print-preview.
Next: none
Assumptions: one continuous A4-width page (min height one A4 page), not split into separate pages.
Status: DONE

### 15:45 — Follow-up: focus trap in the preview
Did: reused the focus-edge pattern from DatasheetsView.vue; added 1 test. Root vitest 329 pass, vue-tsc clean, prettier clean. Committed 3cc5d27, pushed.
Next: none
Assumptions: the focus-edge trap fits better than making the whole app inert, because it matches the existing dialog and needs no teleport.
Status: DONE
