# Verification evidence pilot — 2026-10-26

## Population and scope

Existing Claude browser evidence has 43 events in one agent-config session, 11 actors, 10 tabs and only 14 distinct actor/tab/target contexts. It lacks expected criteria and build identity. Those events were not relabeled as 25 independent attempts.

The prospective pilot instead performed **25 real browser checks** in agent-memory's controlled loopback fixture, through Codex's IAB using `cua_repl`. It used two root-owned tabs on one browser surface; it did not drive other workers' tabs or alter ownership enforcement. Each check freshly loaded the fixture, clicked Increment and observed counter=1 plus available DOM build metadata. The viewport was the unchanged default 1280×720 CSS pixels.

Expected identity came from an independent trusted launch receipt: actual repository, recorded revision, canonical worktree, self-contained server source hash/build ID, instance nonce and loopback origin. The fixture ran from a dirty integration worktree; its build fingerprint binds the self-contained served fixture source, not a production application build or every repository file. Observed identity came from the owned browser tab and DOM, never the model's completion prose.

## Results

| Scenario | Actual checks | Result |
|---|---:|---|
| Correct target and counter predicate | 5 | pass |
| Deliberately stale build marker | 4 | fail: mismatch |
| Wrong expected tab, using another root-owned tab | 4 | fail: mismatch |
| Wrong expected origin/port or worktree | 4 | fail: mismatch |
| Missing build binding | 4 | unknown: incomplete binding |
| Missing criterion | 4 | unknown: criterion missing |

All 25 counters visibly reached 1. Only five checks passed the full contract. Successful navigation/clicks did not turn wrong/missing targets or absent criteria into verified results. The machine-readable artifact contains expected/observed fields, actor/task, exact tab/route, viewport, interaction, predicate, evidence reference, timing, AX observation and assessment for every check. It contains no screenshots or sensitive browser data.

## Measured cost and preservation

- Active browser capture: **11.178 seconds** across 25 checks.
- Local assessment: **0.031 ms** for the saved 25-record batch in one sample; not a production latency prediction.
- Saved JSON artifact: **65,104 bytes** at measurement time.
- No experimental model calls, provider uploads or database writes were performed by the pilot. Production database growth was not measured or claimed.
- Both owned tabs and the temporary server were closed; Port Whisperer found no process on its assigned port.

## Table decision

**Do not create `verification_checks` yet.** The small existing-evidence JSON contract expresses this pilot's bindings and unknowns. A dedicated table could help a real cross-task query workflow, but this controlled fixture does not establish a production criterion producer, human acceptance ground truth or universal browser coverage. Collect that evidence before normalizing it. No screenshot blobs or second timing source was introduced.

This is a contract/behavior pilot, not 25 completed coding tasks, a production false-completion rate or a statistical accuracy estimate. Concurrent worker ownership and authenticated production UI were not exercised. Existing enforcement was preserved.

Evidence: `verification-pilot.json`, `src/agent_memory/verification_evidence.py`, `tools/verification_pilot_server.py`, and the focused predicate/binding tests. One visual observation is retained outside the repository under the chat's visualization directory; it is supporting evidence, not a standalone passing criterion.
