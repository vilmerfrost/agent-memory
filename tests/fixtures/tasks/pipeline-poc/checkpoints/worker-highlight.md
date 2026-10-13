### 15:50 — strong highlight + sheet-click row pulse done
Did: Strong locked (2px solid accent ring + 20% tint) and hover (60% accent ring + 10% tint) states on grid rows (DatasheetEditView.vue) and sheet texts (EditableText.vue); 700 ms row pulse only when editor.start is called from the sheet (arrow steps use internal startEditing, no pulse); reduced-motion disables the pulse; 2 new tests. 328/328 root tests, 460/460 func-node under Node 24, vue-tsc clean, prettier clean. Committed ea6f0a3, pushed feat/datasheets-strong-highlight.
Next: none
Assumptions: no visual check possible (no login/backend); locked caption switched to primary text for contrast in dark theme.
Status: DONE
