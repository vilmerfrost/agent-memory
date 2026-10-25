# Figure request: the build cache

For the caching page of our docs I want one figure that explains how our build cache skips work.
Something quick for the docs: one mechanism, nothing fancy.

How it works:

- Each task's inputs are hashed into a cache key: the source files, the lockfile, the environment and the command.
- If the key matches a stored entry, the task is skipped and its outputs are restored from the store.
- If any one input changes, the key changes and the task runs again.

True numbers from last month's CI (use these, do not invent others):

- Cache hit rate: 82%
- Restore on a hit: 1.2 s
- Full rebuild on a miss: 94 s
- Inputs per key: 4
