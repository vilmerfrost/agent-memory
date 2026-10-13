"""Rule sections with line spans, and splicing one section (docs/edits.md, "Splice").

The rule catalog (rule_catalog.py) names rules; this module finds where each rule's text
starts and ends in one file, with the same rules: a rule runs from its heading to the next
heading that starts another rule. `intro` runs from after the title to the first rule,
`whole` is the file after its frontmatter. Folded deeper headings stay inside their rule.

`sections` checks that its ids and titles equal `rule_catalog.build([text])`, so a span can
never drift from the catalog the deviations were stored with. `splice` replaces exactly one
section and keeps every other byte, including the section's leading and trailing blank lines.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent_memory import rule_catalog
from agent_memory.rule_catalog import INTRO, WHOLE, Rule

NEIGHBOUR_MAX = 2_500


class SectionError(ValueError):
    """The file's sections do not match its rule catalog, or the rule is not in the file."""


@dataclass(frozen=True)
class Section:
    id: str
    title: str
    start: int            # first line (index into text.splitlines(keepends=True))
    end: int              # one past the last line
    heading: str | None   # the heading line as written (None for intro / whole)


def _frontmatter_end(plain: list[str]) -> int:
    if plain and plain[0].strip() == "---":
        for i in range(1, len(plain)):
            if plain[i].strip() in ("---", "..."):
                return i + 1
    return 0


def sections(text: str) -> list[Section]:
    """The file's rules with line spans, in reading order. Raises SectionError when they do
    not match rule_catalog.build([text])."""
    lines = text.splitlines(keepends=True)
    plain = text.splitlines()
    fm_end = _frontmatter_end(plain)
    heads = rule_catalog._headings(plain)
    raw: list[tuple[str, str, int, str | None]] = []   # (id, title, start line, heading)
    if not heads:
        raw.append((WHOLE, "(whole file)", fm_end, None))
    else:
        top = min(level for level, _, _ in heads)
        doc_title: str | None = None
        rest = heads
        if heads[0][0] == top and sum(level == top for level, _, _ in heads) == 1:
            doc_title, rest = heads[0][1], heads[1:]
        if not rest:
            raw.append((WHOLE, doc_title or "(whole file)", fm_end, None))
        else:
            base = min(level for level, _, _ in rest)
            intro_from = heads[0][2] + 1 if doc_title is not None else 0
            if rule_catalog._has_text(rule_catalog._body(plain, intro_from, rest[0][2])):
                raw.append((INTRO, f"{doc_title} (intro)" if doc_title else "(intro)",
                            intro_from if doc_title is not None else fm_end, None))
            parent: tuple[str, str] | None = None
            child_level: int | None = None
            for level, title, i in rest:
                if level <= base or parent is None:
                    parent, child_level = (rule_catalog.slug(title), title), None
                    raw.append((parent[0], title, i, plain[i]))
                elif child_level is None or level <= child_level:
                    child_level = level
                    raw.append((f"{parent[0]}/{rule_catalog.slug(title)}", f"{parent[1]} > {title}", i, plain[i]))
    unique = rule_catalog.unique(Rule(rid, title) for rid, title, _, _ in raw)
    out = []
    for n, (rule, (_, _, start, heading)) in enumerate(zip(unique, raw)):
        end = raw[n + 1][2] if n + 1 < len(raw) else len(lines)
        out.append(Section(rule.id, rule.title, start, end, heading))
    if [Rule(s.id, s.title) for s in out] != rule_catalog.build([text]):
        raise SectionError("section spans do not match the rule catalog")
    return out


def find(text: str, rule_id: str) -> Section:
    for s in sections(text):
        if s.id == rule_id:
            return s
    raise SectionError(f"rule {rule_id!r} is not in the file")


def section_text(text: str, rule_id: str) -> str:
    """The rule's text without its leading and trailing blank lines."""
    s = find(text, rule_id)
    return "".join(text.splitlines(keepends=True)[s.start:s.end]).strip("\r\n")


def neighbours(text: str, rule_id: str) -> tuple[str, str]:
    """(previous rule's text, next rule's text), each capped; '' when there is none."""
    secs = sections(text)
    lines = text.splitlines(keepends=True)
    i = next((n for n, s in enumerate(secs) if s.id == rule_id), None)
    if i is None:
        raise SectionError(f"rule {rule_id!r} is not in the file")

    def body(s: Section) -> str:
        t = "".join(lines[s.start:s.end]).strip("\r\n")
        return t if len(t) <= NEIGHBOUR_MAX else t[:NEIGHBOUR_MAX].rstrip() + "\n[... cut ...]"

    prev = body(secs[i - 1]) if i > 0 else ""
    nxt = body(secs[i + 1]) if i + 1 < len(secs) else ""
    return prev, nxt


def _blank(line: str) -> bool:
    return not line.strip()


def splice(text: str, rule_id: str, new_section: str) -> str:
    """`text` with rule `rule_id` replaced by `new_section`. Only that span changes; its
    leading and trailing blank lines are kept as they were."""
    s = find(text, rule_id)
    lines = text.splitlines(keepends=True)
    old = lines[s.start:s.end]
    lead_n = 0
    while lead_n < len(old) and _blank(old[lead_n]):
        lead_n += 1
    trail_n = 0
    while trail_n < len(old) - lead_n and _blank(old[len(old) - 1 - trail_n]):
        trail_n += 1
    new_lines = new_section.splitlines()
    while new_lines and _blank(new_lines[0]):
        new_lines.pop(0)
    while new_lines and _blank(new_lines[-1]):
        new_lines.pop()
    eol = "\r\n" if old and old[0].endswith("\r\n") else "\n"
    core = eol.join(new_lines) + eol
    last_core = old[len(old) - 1 - trail_n] if len(old) - trail_n > lead_n else ""
    if trail_n == 0 and last_core and not last_core.endswith(("\n", "\r")):
        core = core[: -len(eol)]   # the file had no final newline: keep it that way
    middle = "".join(old[:lead_n]) + core + "".join(old[len(old) - trail_n:])
    return "".join(lines[:s.start]) + middle + "".join(lines[s.end:])
