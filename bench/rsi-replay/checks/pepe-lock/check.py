"""Hidden check for pepe-lock: Phase 3 (Lock) of the pepe skill, after the user declined a suggested third idea.

Graded from ~/.agents/skills/pepe/SKILL.md ("Phase 3 - Lock" and "Briefs"): Pepe already suggested a third idea,
so it accepts the user's choice (exactly the two named ideas); every other idea from the session goes to
parked.md, one line each with its revisit trigger, in the project's idea folder (docs/ideas/ for a confirmed
personal Git project); Lock writes no briefs (they come after Hyper-focus). Stdlib only, no network.
"""
from __future__ import annotations

import json
import pathlib
import re
import sys

IDEAS = ["Ghost Reviewer", "Diff Diary", "PR Weather", "Merge Concierge", "Commit Haiku", "Flake Hunter",
         "Standup Radio", "Ghost Hunter"]
LOCKED = {"ghost reviewer", "diff diary"}
PARKED = {i.lower() for i in IDEAS} - LOCKED


def fail(msg: str) -> None:
    sys.exit(f"pepe-lock: {msg}")


def first_idea(line: str) -> str | None:
    """The idea a parked line is about: the earliest idea name in it (a line may mention its relatives later)."""
    low = line.lower()
    hits = [(low.find(i.lower()), -len(i), i.lower()) for i in IDEAS if i.lower() in low]
    return min(hits)[2] if hits else None


path = pathlib.Path("lock.json")
if not path.is_file():
    fail("lock.json missing")
try:
    lock = json.loads(path.read_text(encoding="utf-8"))
except ValueError as exc:
    fail(f"lock.json is not JSON ({exc})")
locked = lock.get("locked") if isinstance(lock, dict) else None
if not isinstance(locked, list) or not all(isinstance(x, str) for x in locked):
    fail("lock.json needs a list of idea names under 'locked'")
got = {x.strip().lower() for x in locked}
if got != LOCKED or len(locked) != len(LOCKED):
    fail(f"locked {sorted(got)}: after one suggestion the user's choice stands, exactly {sorted(LOCKED)}")

ideas_dir = pathlib.Path("docs/ideas")
parked = ideas_dir / "parked.md"
if not parked.is_file():
    fail("docs/ideas/parked.md missing (a confirmed personal Git project keeps ideas in docs/ideas/)")
rows = [ln.strip() for ln in parked.read_text(encoding="utf-8").splitlines()]
rows = [ln for ln in rows if ln and not ln.startswith("#")]
seen: list[str] = []
for ln in rows:
    idea = first_idea(ln)
    if idea is None:
        fail(f"parked.md line names no idea from the session: {ln!r}")
    if idea in LOCKED:
        fail(f"a locked idea is parked: {ln!r}")
    if not re.search(r"revisit", ln, re.I):
        fail(f"parked line has no revisit trigger: {ln!r}")
    seen.append(idea)
if sorted(seen) != sorted(PARKED):
    fail(f"parked.md must hold every other session idea once, one line each; got {sorted(seen)}, "
         f"want {sorted(PARKED)}")
extra = sorted(p.name for p in ideas_dir.rglob("*") if p.is_file() and p != parked)
if extra:
    fail(f"Lock writes no briefs yet; found {extra}")
print("ok")
