@AGENTS.md

## Full integration tests

Run the final gate from the managed integration checkout after committing the changes:

```bash
cd ~/worktrees/agent-memory/_integration
parent-integrate test
```

The local parent profile selects `scripts/integration_tests.py`. It runs the full
suite across four pytest processes, keeps tests from each file together, and gives
each process its own disposable Postgres container. Only `parent-integrate test`
records passing evidence for the clean commit and configuration.

For a full run in another managed worktree:

```bash
parent-run -- uv run --locked --quiet python -u scripts/integration_tests.py
```

### When a test fails

- Let the run collect the remaining failures. The runner reports failures; it does not automatically fix or retry them.
- Fix the cause, then rerun the specific failed test or use the command below in the same checkout:

```bash
parent-run -- uv run --locked pytest -q --lf --last-failed-no-failures none
```

- Check that the intended tests actually ran. An empty failure cache proves nothing.
- Once the focused checks pass, commit the fix and run the full integration gate once.
- Do not resume halfway and combine results from different code versions. A changed commit needs a complete passing run.
- Do not weaken assertions, raise timeouts, add skips, or add retries merely to obtain a pass.

### Live progress and saved logs

- Keep command output visible. Do not pipe the run through `tail -15` or wait until it finishes before checking progress.
- The runner prints elapsed time, completed/total tests, and outcome counts every five seconds. Failures appear immediately.
- If the command runs asynchronously, check its output while it is running so Vilmer can see progress.
- Complete verbose pytest output, including failure tracebacks and captured failure output, is saved in this worktree's Git metadata under `test-logs/`.
- The runner prints the exact log path and a `tail -f` command. `latest.log` in that directory points to the latest run.
- A failed run keeps pytest's nonzero exit code and produces no passing integration receipt.

The verified integration run after this change took about 93 seconds, with 2,673
passed and 3 existing skips. This is a measured reference, not a runtime guarantee.
