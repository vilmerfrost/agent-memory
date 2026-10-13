### 16:40 — Read references
Did: renamed branch to feat/datasheets-purmo-look; read main.css, data-sheet.html, our DatasheetSheet.vue, editor usage, tests; rendered both PDFs to PNG.
Next: download font + logo into public/purmo, restyle DatasheetSheet.vue.
Assumptions: keep h2 section headings (tests and outline) but style them like Purmo's h1; texts heading visually hidden on the full sheet, visible in compact.

### 16:50 — Implemented, printed, pushed
Did: merged origin/feat/datasheets-item-table-groups (fast-forward); restyled DatasheetSheet.vue; added datasheet-print-page.ts (@page margin boxes); copied fonts and logo; printed Kos V fixture with headless Chromium 156 to /tmp/worker-design-kos-v.pdf; vitest 356/356, vue-tsc clean, prettier clean, vite build ok. Committed 96fedb6, pushed.
Next: none
Assumptions: the UniNeue licence allows us to host it (the parent's decision; flagged as open).
Status: DONE
