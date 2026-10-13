"""The kind check (edits_kind.classify) on synthetic section diffs. No database, no CLI."""

from __future__ import annotations

import json

import pytest

from agent_memory import edits_kind
from agent_memory.edits_writer import parse_proposal
from test_edits_support import proposal

# The two real-world shapes from the 2026-10-13 live run, both labelled `hop` by the writer.
SHELL_OLD = """## Shell Habits

- The shell is zsh. Quote every argument that holds `*`, `?` or `[` (for example `--include='*.py'`), or zsh
  stops with "no matches found" before the command runs. <!-- improve:zsh-quote-globs -->
- Run any command you expect to take over 2 minutes (full test suites, builds, `docker compose`, eval runs)
  with `run_in_background`, then wait for its notification. Never block a turn on it in the foreground.
  <!-- improve:background-long-commands -->
"""
SHELL_NEW = """## Shell Habits

- The shell is zsh. Quote every argument that holds `*`, `?` or `[` (for example `--include='*.py'`), or zsh
  stops with "no matches found" before the command runs. <!-- improve:zsh-quote-globs -->
- Run any command you expect to take over 2 minutes (full test suites, builds, `docker compose`, eval runs)
  with `run_in_background`, then wait for its notification. Never block a turn on it in the foreground.
  This also covers polling: do not write a foreground sleep loop that waits for a file, a port or a log
  line. Start the wait in the background, then stay idle until its notification arrives.
  <!-- improve:background-long-commands -->
"""

DESIGN_OLD = """## Design System

**YOU MUST read `~/docs/design/system.md` before doing any design work.** Design work includes:
- slide decks, slideshows and presentations
- artifacts and HTML pages
- UI, frontend and visual design

Read the file at the start of the task. Follow it.
"""
DESIGN_NEW = """## Design System

**YOU MUST read `~/docs/design/system.md` before doing any design work.** Design work includes:
- slide decks, slideshows and presentations
- artifacts and HTML pages
- UI, frontend and visual design
- diagrams, charts and inline visual widgets
- any other visual output

Read the file before your first design step, even for a small visual. Follow it.
"""

STEPS_OLD = "## Ship\n1. Run the tests.\n2. Merge the branch.\n3. Push.\n"


def kind(old: str, new: str, writer: str | None = None) -> edits_kind.Check:
    return edits_kind.classify(old, new, writer)


def test_real_shape_bullet_extended_with_prose_is_prose() -> None:
    c = kind(SHELL_OLD, SHELL_NEW, "hop")
    assert c.kind == "prose" and c.hop == [] and c.contract == [], c
    assert c.reason == "no step-order change and no new output or check clause"


def test_real_shape_scope_widened_and_read_before_is_not_hop() -> None:
    c = kind(DESIGN_OLD, DESIGN_NEW, "hop")
    assert c.kind in ("contract", "prose") and c.kind != "hop" and c.hop == [], c


@pytest.mark.parametrize("new,cue", [
    (STEPS_OLD + "The final report must include the test command and its exit code.\n", "required output clause"),
    ("## Ship\n1. Run the tests.\n2. Merge the branch.\n3. Push. The step is done only when the canary is green.\n",
     "done condition"),
    (STEPS_OLD + "Verify that the deploy URL answers 200 before you report.\n", "check clause"),
    (STEPS_OLD + "End the report with an `Evidence:` line.\n", "report field"),
])
def test_contract_cues(new: str, cue: str) -> None:
    c = kind(STEPS_OLD, new, "contract")
    assert c.kind == "contract" and cue in c.contract, c
    assert c.reason.startswith("new or sharpened ")


def test_sharpened_output_clause_is_contract() -> None:
    old = "## Brief\nEach brief names the branch.\n"
    c = kind(old, "## Brief\nEach brief names the branch and the base commit, on its first line.\n", "prose")
    assert c.kind == "contract" and c.contract == ["output content clause"]


@pytest.mark.parametrize("new,cue", [
    ("## Ship\n1. Merge the branch.\n2. Run the tests.\n3. Push.\n", "existing lines reordered"),
    ("## Ship\n1. Run the tests.\n2. Merge the branch.\n3. Rebuild the docs.\n4. Push.\n",
     "step inserted into the numbered sequence"),
    (STEPS_OLD + "After the canary passes, then announce the release.\n", "after-then order"),
    (STEPS_OLD + "First tag the release, then write the notes.\n", "first-then order"),
    (STEPS_OLD + "Push only after the review is approved.\n", "only-after order"),
    (STEPS_OLD + "If the tests fail, go back to the plan.\n", "loop-back"),
    (STEPS_OLD + "Run the smoke test before step 3.\n", "before/after step N"),
])
def test_hop_cues(new: str, cue: str) -> None:
    c = kind(STEPS_OLD, new, "hop")
    assert c.kind == "hop" and cue in c.hop, c
    assert c.reason.startswith("step order changed: ")


def test_bullets_newly_numbered_are_a_hop() -> None:
    old = "## Ship\n- Run the tests.\n- Merge the branch.\n"
    c = kind(old, "## Ship\n1. Run the tests.\n2. Merge the branch.\n", "hop")
    assert c.kind == "hop" and "steps newly numbered" in c.hop


@pytest.mark.parametrize("new", [
    "## Ship\n1. Run the whole test suite, not a subset.\n2. Merge the branch.\n3. Push.\n",   # reworded step
    STEPS_OLD + "4. Clean up the worktree.\n",                                                 # appended step
    STEPS_OLD + "Keep commits small, then the review stays quick.\n",                          # bare ", then"
    STEPS_OLD + "Read the release notes before your first push.\n",                            # precondition
    "## Ship\n1. Run the\n   tests.\n2. Merge the branch.\n3. Push. Keep it calm.\n",            # rewrap + prose
])
def test_prose_shapes(new: str) -> None:
    c = kind(STEPS_OLD, new, "hop")
    assert c.kind == "prose" and c.hop == [] and c.contract == [], c


def test_old_cues_do_not_count() -> None:
    old = "## Ship\nAfter the tests pass, then merge.\nThe report must include the exit code.\n"
    new = "## Ship\nAfter the tests pass, then merge.\nThe report must include the exit code.\nStay calm about flakes.\n"
    assert kind(old, new, "hop").kind == "prose"


@pytest.mark.parametrize("writer,want", [("contract", "contract"), ("hop", "hop"), ("prose", "contract")])
def test_writer_breaks_a_contract_hop_tie(writer: str, want: str) -> None:
    new = STEPS_OLD + "After the tests pass, then merge; the report must include the exit code.\n"
    c = kind(STEPS_OLD, new, writer)
    assert c.contract and c.hop and c.kind == want


def test_mismatch_is_recorded_on_the_proposal() -> None:
    text = "# Global\n\n" + SHELL_OLD + "\n## Other\nText.\n"
    new_section = SHELL_NEW.strip("\n")
    p = parse_proposal(json.dumps(proposal(new_section, topic="background long commands", kind="hop",
                                           kind_reason="Adds a loop-back from polling to the background wait.")),
                       file_text=text, rule_id="shell-habits", cand={"instruction_source": "claude-md:global",
                                                                     "rule_id": "shell-habits"})
    assert p.kind_writer == "hop" and p.kind == "prose" and p.kind_mismatch
    assert p.kind_reason == "Adds a loop-back from polling to the background wait."
    assert p.kind_shown == "no step-order change and no new output or check clause; writer said hop"
