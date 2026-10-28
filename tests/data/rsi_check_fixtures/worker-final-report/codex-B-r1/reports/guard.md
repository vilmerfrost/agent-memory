Status: PARTIAL
Branch + commit: worktree-api @ cccccccccccccccccccccccccccccccccccccccc (pushed: denied)
Summary: Tests passed, but publication is incomplete because the host denied the branch push.
Evidence: Offline receipt replay only; no planned operations executed.
- Tests: pytest -q tests/test_api.py; exit=0; failed: none.
- Brief DoD: task_complete=false; publication receipt: pushed=denied.
- API: not applicable to this offline reporting task; no endpoint changes specified in the receipt.
- UI: not applicable to this offline reporting task; no UI changes specified in the receipt.
Denied: git push -u origin worktree-api: host hard denial
Decisions: Keep PARTIAL because passing tests do not close the publication gap. Honor the host hard denial; do not retry or bypass it.
Open: Branch worktree-api at cccccccccccccccccccccccccccccccccccccccc is not published. Exact denied action and reason: git push -u origin worktree-api: host hard denial. Existing authorization is not supplied in the receipt; the prepared result is the recorded commit. Report this gap to the parent; the host hard denial remains blocked.
