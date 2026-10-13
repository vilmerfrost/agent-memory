"""Section spans and splicing: only the one rule's text changes. No database."""

from __future__ import annotations

import pytest

from agent_memory import edits_sections, rule_catalog
from test_edits_support import GLOBAL_MD, SKILL_MD

COMPLEX = """---
title: x
---
# Title

Intro line.

## 1. First
Text one.
still first

## Second
### Child A
a
```
## not a heading in code
```
### Child B
b
#### deeper
b2

## Notes
n1

## Notes
n2
"""


@pytest.mark.parametrize("text", [GLOBAL_MD, SKILL_MD, COMPLEX, "plain routine prompt\nno headings\n",
                                  "# Only a title\nbody\n", "## a\nx\n## b\ny"])
def test_sections_match_the_catalog(text: str) -> None:
    secs = edits_sections.sections(text)
    assert [rule_catalog.Rule(s.id, s.title) for s in secs] == rule_catalog.build([text])
    lines = text.splitlines(keepends=True)
    # Spans are ordered and never overlap.
    assert all(a.end <= b.start for a, b in zip(secs, secs[1:]))
    for s in secs:
        if s.heading is not None:
            assert lines[s.start].rstrip("\n") == s.heading


def test_complex_spans() -> None:
    by = {s.id: s for s in edits_sections.sections(COMPLEX)}
    assert list(by) == ["intro", "first", "second", "second/child-a", "second/child-b", "notes", "notes-2"]
    assert edits_sections.section_text(COMPLEX, "first") == "## 1. First\nText one.\nstill first"
    assert edits_sections.section_text(COMPLEX, "second") == "## Second"
    assert "## not a heading in code" in edits_sections.section_text(COMPLEX, "second/child-a")
    assert edits_sections.section_text(COMPLEX, "notes-2") == "## Notes\nn2"
    assert edits_sections.section_text(COMPLEX, "intro") == "Intro line."


@pytest.mark.parametrize("rule_id", ["intro", "first", "second", "second/child-a", "second/child-b", "notes",
                                     "notes-2"])
def test_splice_changes_only_that_section(rule_id: str) -> None:
    s = edits_sections.find(COMPLEX, rule_id)
    lines = COMPLEX.splitlines(keepends=True)
    old = edits_sections.section_text(COMPLEX, rule_id)
    new_sec = old + "\nOne more line."
    out = edits_sections.splice(COMPLEX, rule_id, new_sec)
    before, after = "".join(lines[:s.start]), "".join(lines[s.end:])
    assert out.startswith(before) and out.endswith(after)
    assert out == before + "".join(lines[s.start:s.end]).replace(old, new_sec, 1) + after
    assert rule_catalog.build([out]) == rule_catalog.build([COMPLEX])


def test_splice_keeps_blank_lines_and_missing_final_newline() -> None:
    text = "## a\nx\n\n\n## b\ny"
    out = edits_sections.splice(text, "a", "\n## a\nx2\n\n")
    assert out == "## a\nx2\n\n\n## b\ny"
    out = edits_sections.splice(text, "b", "## b\ny2\n")
    assert out == "## a\nx\n\n\n## b\ny2"


def test_splice_whole_keeps_frontmatter() -> None:
    text = "---\nname: r\n---\nRun the suite.\n"
    assert edits_sections.splice(text, "whole", "Run the suite twice.") == "---\nname: r\n---\nRun the suite twice.\n"


def test_neighbours_and_missing_rule() -> None:
    prev, nxt = edits_sections.neighbours(SKILL_MD, "spawn")
    assert prev == "Intro text for the skill." and nxt.startswith("## 5. Track")
    assert edits_sections.neighbours(SKILL_MD, "intro")[0] == ""
    with pytest.raises(edits_sections.SectionError):
        edits_sections.find(SKILL_MD, "nope")
