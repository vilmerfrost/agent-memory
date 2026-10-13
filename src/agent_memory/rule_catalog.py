"""Rule catalog: stable ids for the rules inside one instruction source (docs/deviations.md).

A deviation names a rule inside an instruction file (a skill, a CLAUDE.md, PARENT.md, ...).
The writer used to name it in free text, so one rule showed up under several spellings and
recurrence never added up. The catalog turns each instruction file into a numbered list of
rules with a stable id, the writer must answer with one of those ids, and `snap` maps any
other spelling (old rows, a sloppy reply) onto the best id or `unmatched`.

Granularity (parse_markdown):

- Headings are ATX headings (`#` .. `######`) outside fenced code blocks and outside YAML
  frontmatter. Setext headings are not headings here.
- A file whose first heading is its only heading at the shallowest level has a title (the
  usual `# Name`). The title is not a rule; the next level down is the base level.
  Otherwise the shallowest level is the base level.
- Each base-level heading is a rule. The first deeper level under it is a child rule
  (`parent/child` id). Anything deeper, and bullets or text, belongs to the nearest rule.
- Text between the title and the first rule is one rule `intro`.
- A file without rule headings (a routine prompt, a short note) is one rule `whole`.
- Ids are slugs of the heading text without a leading step number (`## 4. Spawn` ->
  `spawn`, `## 0b. Profile` -> `profile`), so renumbering steps keeps the ids. The title keeps
  the heading text as written (`4. Spawn`; a child is `Parent > Child`).
- A repeated id gets `-2`, `-3`, ... in reading order.

A source made of several files (a repo's CLAUDE.md + CLAUDE.local.md) is the files'
catalogs one after the other, ids made unique the same way.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from difflib import SequenceMatcher

WHOLE = "whole"
INTRO = "intro"
UNMATCHED = "unmatched"
SNAP_THRESHOLD = 0.8          # below this a ref is `unmatched`
SEGMENT_MAX = 40              # chars per id segment, so an id stays well under REF_MAX

EXACT, SNAPPED = "exact", "snapped"


@dataclass(frozen=True)
class Rule:
    id: str
    title: str


@dataclass(frozen=True)
class Match:
    rule_id: str           # a catalog id or UNMATCHED
    rule_title: str | None
    how: str               # exact | snapped | unmatched
    score: float

    def meta(self) -> dict[str, object]:
        return {"ref_match": self.how, "ref_score": round(self.score, 3)}


# --------------------------------------------------------------------------- parsing

_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_FENCE_RE = re.compile(r"^[ \t]{0,3}(```|~~~)")
_NUMBER_RE = re.compile(r"^(?:step\s+)?\d+[a-z]?[.):]\s*", re.I)
_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")


def clean_title(text: str) -> str:
    text = _LINK_RE.sub(r"\1", text)
    text = text.replace("`", "").replace("**", "").replace("__", "")
    return " ".join(text.split())


def strip_number(title: str) -> str:
    rest = _NUMBER_RE.sub("", title, count=1)
    return rest if re.search(r"[A-Za-z0-9]", rest) else title


def slug(text: str) -> str:
    text = unicodedata.normalize("NFKD", strip_number(clean_title(text)))
    text = text.encode("ascii", "ignore").decode().lower().replace("'", "")
    out = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return out[:SEGMENT_MAX].rstrip("-") or "section"


def _headings(lines: list[str]) -> list[tuple[int, str, int]]:
    """(level, title, line index) per heading. Skips frontmatter and fenced code."""
    start = 0
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() in ("---", "..."):
                start = i + 1
                break
    out: list[tuple[int, str, int]] = []
    fence: str | None = None
    for i in range(start, len(lines)):
        line = lines[i]
        m = _FENCE_RE.match(line)
        if m:
            if fence is None:
                fence = m.group(1)
            elif m.group(1) == fence:
                fence = None
            continue
        if fence is not None:
            continue
        h = _HEADING_RE.match(line)
        if h:
            title = clean_title(h.group(2))
            if title:
                out.append((len(h.group(1)), title, i))
    return out


def _has_text(lines: list[str]) -> bool:
    return any(ln.strip() and not ln.strip().startswith("<!--") for ln in lines)


def parse_markdown(text: str) -> list[Rule]:
    """The rules of one file, in reading order (ids not yet unique across files)."""
    all_lines = text.splitlines()
    heads = _headings(all_lines)
    if not heads:
        return [Rule(WHOLE, "(whole file)")]
    top = min(level for level, _, _ in heads)
    doc_title: str | None = None
    rest = heads
    if heads[0][0] == top and sum(level == top for level, _, _ in heads) == 1:
        doc_title, rest = heads[0][1], heads[1:]
    if not rest:
        return [Rule(WHOLE, doc_title or "(whole file)")]
    base = min(level for level, _, _ in rest)
    rules: list[Rule] = []
    intro_from = heads[0][2] + 1 if doc_title is not None else 0
    if _has_text(_body(all_lines, intro_from, rest[0][2])):
        rules.append(Rule(INTRO, f"{doc_title} (intro)" if doc_title else "(intro)"))
    parent: tuple[str, str] | None = None   # (id, title) of the current base rule
    child_level: int | None = None
    for level, title, _ in rest:
        if level <= base or parent is None:
            parent, child_level = (slug(title), title), None
            rules.append(Rule(parent[0], title))
        elif child_level is None or level <= child_level:
            child_level = level
            rules.append(Rule(f"{parent[0]}/{slug(title)}", f"{parent[1]} > {title}"))
        # deeper headings fold into the current rule
    return rules


def _body(lines: list[str], start: int, end: int) -> list[str]:
    body = lines[start:end]
    if start == 0 and body and body[0].strip() == "---":  # frontmatter before the first heading
        for i in range(1, len(body)):
            if body[i].strip() in ("---", "..."):
                return body[i + 1:]
    return body


def unique(rules: Iterable[Rule]) -> list[Rule]:
    """Make ids unique in reading order: the second `notes` becomes `notes-2`."""
    seen: set[str] = set()
    out: list[Rule] = []
    for r in rules:
        rid, n = r.id, 1
        while rid in seen:
            n += 1
            rid = f"{r.id}-{n}"
        seen.add(rid)
        out.append(Rule(rid, r.title))
    return out


def build(texts: Iterable[str]) -> list[Rule]:
    """The catalog of one instruction source made of one or more files."""
    rules: list[Rule] = []
    for t in texts:
        rules.extend(parse_markdown(t))
    return unique(rules)


def sha(texts: Iterable[str]) -> str:
    """sha256 of the source's files as read (joined like load_instructions), for before/after an edit."""
    return hashlib.sha256("\n\n".join(texts).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- snapping


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", strip_number(clean_title(text)))
    text = text.encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


_PATH_SPLIT_RE = re.compile(r"\s*(?:>|»|/)\s*")
_SEGMENT_SPLIT_RE = re.compile(r"\s*(?::|;|,|\(|\)|\s/\s|\s[-–—]\s|\|)\s*")


def _candidates(ref: str) -> list[str]:
    """The ref as a whole, as a heading path, then its first segment (writer prefixes and
    suffixes like `Safety guards: never read ...` or `10. Record / 9. Clean`)."""
    out = [ref]
    parts = [p for p in _PATH_SPLIT_RE.split(ref) if p.strip()]
    if len(parts) > 1:
        out.append("/".join(slug(p) for p in parts))
    segs = [s for s in _SEGMENT_SPLIT_RE.split(ref) if s.strip()]
    if len(segs) > 1:
        out.append(segs[0])
    if len(parts) > 1:
        out.append(parts[0])
    return out


def _score(cand: str, rule: Rule) -> float:
    if cand.casefold() == rule.id.casefold():
        return 1.0
    c = _norm(cand)
    if not c:
        return 0.0
    if slug(cand) in (rule.id, rule.id.rsplit("/", 1)[-1]):
        return 1.0
    titles = {_norm(rule.title), _norm(rule.title.rsplit(" > ", 1)[-1])}
    best = 0.0
    for t in titles:
        if not t:
            continue
        if c == t:
            return 1.0
        if len(t) >= 4 and c.startswith(t + " "):
            best = max(best, 0.92)
        elif len(c) >= 4 and t.startswith(c + " "):
            best = max(best, 0.85)
        else:
            best = max(best, SequenceMatcher(None, c, t).ratio())
    return best


def snap(ref: str, catalog: list[Rule], threshold: float = SNAP_THRESHOLD) -> Match:
    """Map a free-text ref onto one catalog rule.

    Exact id (case-insensitive) -> `exact`. Else the best title/slug similarity at or above
    `threshold` -> `snapped`. A catalog of one rule (`whole`) takes every ref. Anything else,
    and a tie between two rules, -> `unmatched`.
    """
    ref = (ref or "").strip()
    if not catalog:
        return Match(UNMATCHED, None, UNMATCHED, 0.0)
    for r in catalog:
        if ref.casefold() == r.id.casefold():
            return Match(r.id, r.title, EXACT, 1.0)
    if len(catalog) == 1:
        r = catalog[0]
        return Match(r.id, r.title, SNAPPED, 1.0)
    best: tuple[float, Rule] | None = None
    for cand in _candidates(ref):
        scored = sorted(((_score(cand, r), i) for i, r in enumerate(catalog)), key=lambda x: (-x[0], x[1]))
        top, i = scored[0]
        if top < threshold:
            continue
        if len(scored) > 1 and scored[1][0] == top:
            continue  # two rules fit this spelling equally well: not this candidate
        if best is None or top > best[0]:
            best = (top, catalog[i])
    if best is None:
        return Match(UNMATCHED, None, UNMATCHED, 0.0)
    return Match(best[1].id, best[1].title, SNAPPED, best[0])


def rule_key(rule_id: str | None, ref: str) -> str:
    """The recurrence key inside one source: the rule id, or the lowercased ref for rows that
    have none yet (before `reref`) or are unmatched. Ids never hold `:`, so keys never clash."""
    if rule_id and rule_id != UNMATCHED:
        return rule_id
    return "ref:" + ref.lower()


RULE_KEY_SQL = "coalesce(nullif(d.rule_id, 'unmatched'), 'ref:' || lower(d.instruction_ref))"
