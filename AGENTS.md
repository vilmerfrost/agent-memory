# Future work

When Vilmer asks what remains, what to improve, or what to work on next, read
`roadmap.md`. It records deferred ideas and their scope; it does not authorize
implementation or release.

# Testing and visible progress

- Read `~/.claude/parent/agent-memory/profile.md` when using the local parent workflow. It owns the integration test command.
- If `parent-run` refuses a test command (for example, setup not verified), list it under `Denied:` in your report and keep working on other parts. Run the tests once the root has fixed setup.
- For the final full integration gate, run `parent-integrate test` from the managed `_integration` checkout after committing the fix. Only this command records a passing result for the clean commit and configuration.
- In other managed worktrees, run `parent-run -- uv run --locked --quiet python -u scripts/integration_tests.py`. It runs all tests across four processes, keeps each file together, prints progress every five seconds, and saves full pytest output in that worktree's Git metadata.
- Keep the command's live output visible. Do not pipe it to `tail -15` or wait until completion before checking progress. The runner prints its full log path; `tail -f <printed-path>` follows the detailed log during the run.
- Ordinary test failures are collected while the rest of the suite runs. Fix the cause, then rerun the specific failed test or use `parent-run -- uv run --locked pytest -q --lf --last-failed-no-failures none` in the same checkout. Verify that the intended tests actually ran; an empty failure cache proves nothing.
- After the focused checks pass, commit and run the complete integration gate once. Do not treat a failed-only run, a resumed run, or results from an older commit as full integration acceptance.
- Preserve assertions, intentional timeout checks, environment isolation and cleanup. Do not add retries, skips or weaker expectations just to get a green result.

See the Tests section of `README.md` for serial debugging and log details.

The runner also saves a `.timings.json` batch beside each log. Under `parent-run`,
the collector uploads per-test setup/call/teardown durations and outcomes to
Supabase after the run. Use `amem timing tests --run '<printed-run-id>'` to find
the slowest tests. Details expire after 30 days; summaries remain. See
`docs/session-timing.md` for interpretation and retry behavior.
