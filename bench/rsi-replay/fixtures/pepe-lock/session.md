# Pepe session: what to build next for `shipit` (a CLI that reviews and merges small PRs)

Project: confirmed personal Git project, owned by the user. The repo root is this folder. Its project
policy allows idea files under `docs/ideas/`. No briefs exist yet.

---

**Pepe (pitch, round 1)**

- **Safe — PR Weather**: a one-line forecast per PR: likely to merge today, stuck, or blocked. Clicks when you
  open your review queue in the morning.
- **Bold — Merge Concierge**: shipit merges every green, approved PR on its own and writes the changelog.
- **Weird — Commit Haiku**: every squash commit message rewritten as a haiku. It is still a real summary.
- **Stolen — Diff Diary**: the flight-recorder pattern from aviation: a daily log of what changed and why,
  written from the diffs.
- **Agent-native — Ghost Reviewer**: an agent that leaves a first review in your voice before a human looks.

My pick: **Ghost Reviewer**, because review latency is the bottleneck in your git log. I'd change my mind if
your PRs are mostly one-line fixes.

Which of these pull at you, or what's actually on your mind?

**User**: Ghost Reviewer and Diff Diary for sure. Merge Concierge scares me. Haiku is a joke, drop it.

**Pepe (riff)**

- Push: **Flake Hunter** — reruns flaky tests on a PR and names the flake before a reviewer wastes time.
- Mutate: **Standup Radio** — Diff Diary read out as a 60-second audio briefing.
- Mashup: **Ghost Hunter** — Ghost Reviewer × Flake Hunter: the first review also says which failures are flakes.

The board:
1. Ghost Reviewer
2. Diff Diary
3. PR Weather
4. Flake Hunter
5. Standup Radio
6. Ghost Hunter

**User**: lock Ghost Reviewer and Diff Diary.

**Pepe**: That is two. I'd add Flake Hunter as a third: it is cheap and makes Ghost Reviewer's reviews trustworthy.
Lock these 3?

**User**: No. Just those two. Lock it.
