### 15:43 — Text zoom done, committed and pushed
Did: DatasheetEditView lays the paper out at min(794, floor(paneWidth / zoom)) for Text 100%/125% (Fit unchanged), centres it, adds scrollbar-gutter: stable, relabels options "Text 100%/125%". 5 tests in "Datasheet editor" (RO stub). Root vitest 330 pass, func-node 460 pass (Node 24), vue-tsc clean, prettier clean. Pushed feat/datasheets-text-zoom @ 69f9fad.
Next: none
Assumptions: paper never wider than A4 on a wide pane (centred, not stretched); visual check not possible in worktree.
Status: DONE
