"""Check the writer's `kind` label against the actual diff of the section (docs/edits.md,
"Kind check").

The writer labels each edit contract, hop or prose. Its label is not trusted on its own: a
live run labelled a longer bullet and a wider scope list as `hop`. `classify` looks at what
the edit really changed:

- hop: existing lines moved (their order changed), plain lines turned into numbered steps,
  a new numbered step put in front of an existing one, or new text with explicit step order
  ("after X, then Y", "first X, then Y", "only after", "before step 3", a loop-back such as
  "go back to step 2").
- contract: new text that adds or sharpens a required-output or check clause ("must report
  X", "the reply names Y", "done only when Z", "verify that", "must pass", an `Evidence:`
  line).
- prose: everything else (rewording, a wider or narrower scope list, an explanation, a
  precondition like "read X before your first Y").

A cue counts only when the new words of the edit fall inside it, so a cue that was already
in the old text does not count. When the diff supports both contract and hop, the writer's
label decides between them; with no support for either, the checked label is prose.

Limits: keyword cues, English only. A moved line that was also reworded counts as a new
line, not as a move. A plain "X, then Y" without "after/first/once" is not counted as a hop.
A required output written without the cue words reads as prose.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

KINDS = ("contract", "hop", "prose")

_LIST_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_NUM_RE = re.compile(r"^\s*\d+[.)]\s+")
_WORD_RE = re.compile(r"\S+")
_CLAUSE = r"[^.;\n]*"

# Each cue: (compiled pattern, short reason). The match must hold at least one new character.
_OUTPUT_VERBS = (r"(?:return|report|output|print|include|list|name|state|show|produce|contain|record|cite|attach|"
                 r"carry|end with|start with|give)")
_OUTPUT_NOUNS = (r"(?:brief|report|reply|answer|summary|output|result|response|commit message|pr body|"
                 r"pr description|message|checkpoint|handoff|final report|final message)")
CONTRACT_CUES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(rf"\b(?:must|should|has to|have to|needs? to|always)\s+{_OUTPUT_VERBS}s?\b{_CLAUSE}", re.I),
     "required output clause"),
    (re.compile(rf"\b(?:the|each|every|your|its|a|an)\s+(?:final\s+)?{_OUTPUT_NOUNS}\s+(?:must\s+|should\s+)?"
                rf"(?:names?|includes?|lists?|states?|shows?|contains?|carries|ends? with|starts? with|holds?|"
                rf"gives?|says|returns?|reports?)\b{_CLAUSE}", re.I),
     "output content clause"),
    (re.compile(rf"\b(?:is\s+)?(?:done|complete|finished)\s+(?:only\s+)?(?:when|once|if)\b{_CLAUSE}", re.I),
     "done condition"),
    (re.compile(rf"\b(?:counts? as done|not done until|done-?check|definition of done)\b{_CLAUSE}", re.I),
     "done condition"),
    (re.compile(rf"\b(?:check|verify|confirm|assert|prove)\s+(?:that|whether)\b{_CLAUSE}", re.I), "check clause"),
    (re.compile(rf"\b(?:must|should|has to|needs? to)\s+(?:pass|succeed|exit 0|be green|match)\b{_CLAUSE}", re.I),
     "pass condition"),
    (re.compile(r"\bexit (?:code|status)\b", re.I), "pass condition"),
    (re.compile(r"[`\"']?\b[A-Z][\w ]{0,20}:[`\"']?\s+(?:field|line|entry)\b"), "report field"),
    (re.compile(rf"\b(?:fails?|refuses?|stops?)\s+(?:if|when|unless)\b{_CLAUSE}", re.I), "check clause"),
)
HOP_CUES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(?:after|once)\b[^.;\n]{1,100}?\bthen\b", re.I), "after-then order"),
    (re.compile(r"\bfirst\b[^.;\n]{1,100}?\bthen\b", re.I), "first-then order"),
    (re.compile(r"\b(?:only after|only once|not before|never before)\b", re.I), "only-after order"),
    (re.compile(r"\b(?:before|after)\s+(?:step|phase)\s*\d+", re.I), "before/after step N"),
    (re.compile(r"\b(?:step|phase)\s*\d+\s+(?:comes|runs|goes|happens)\s+(?:before|after)\b", re.I),
     "step order"),
    (re.compile(r"\b(?:go|loop|jump|return|come)\s+back\s+to\b", re.I), "loop-back"),
    (re.compile(r"\bloop-?back\b", re.I), "loop-back"),
    (re.compile(r"\b(?:repeat|restart|re-?run)\s+(?:from\s+)?(?:step|phase)\b", re.I), "loop-back"),
)


@dataclass
class Check:
    kind: str                                         # the checked label
    reason: str                                       # one line: what in the diff decided it
    contract: list[str] = field(default_factory=list)  # contract evidence found
    hop: list[str] = field(default_factory=list)       # hop evidence found


def _blocks(section: str) -> list[str]:
    """Logical lines: a bullet or numbered item with its continuation lines, a paragraph, a
    heading. Soft-wrapped text becomes one line, so a rewrap is not a change."""
    out: list[str] = []
    cur: list[str] = []
    for line in section.splitlines():
        s = line.strip()
        starts = not s or s.startswith("#") or _LIST_RE.match(line) or s.startswith(("|", "```", "<!--"))
        if starts and cur:
            out.append(" ".join(cur))
            cur = []
        if s:
            cur.append(" ".join(s.split()))
        if s.startswith("#") and cur:
            out.append(" ".join(cur))
            cur = []
    if cur:
        out.append(" ".join(cur))
    return out


def _norm(block: str) -> str:
    return " ".join(_LIST_RE.sub("", block).lower().split())


def _token_key(tok: str) -> str:
    return tok.lower().strip(".,;:!?()[]{}\"'`*_")


def _new_chars(old_text: str, new_text: str) -> set[int]:
    """Character offsets of `new_text` that belong to words the edit inserted or replaced."""
    old_toks = [m.group(0) for m in _WORD_RE.finditer(old_text)]
    new_m = list(_WORD_RE.finditer(new_text))
    sm = difflib.SequenceMatcher(None, [_token_key(t) for t in old_toks],
                                 [_token_key(m.group(0)) for m in new_m], autojunk=False)
    marked: set[int] = set()
    for op, _i1, _i2, j1, j2 in sm.get_opcodes():
        if op in ("insert", "replace"):
            for m in new_m[j1:j2]:
                marked.update(range(m.start(), m.end()))
    return marked


def _cues(cues: tuple[tuple[re.Pattern[str], str], ...], text: str, new: set[int]) -> list[str]:
    found: list[str] = []
    for rx, label in cues:
        for m in rx.finditer(text):
            if any(i in new for i in range(m.start(), m.end())) and label not in found:
                found.append(label)
                break
    return found


def _structure(old_blocks: list[str], new_blocks: list[str]) -> list[str]:
    """Hop evidence from the block structure: moved lines, newly numbered steps, a step put in
    front of an existing numbered step."""
    found: list[str] = []
    old_n = [_norm(b) for b in old_blocks]
    new_n = [_norm(b) for b in new_blocks]
    once_old = {b for b in old_n if old_n.count(b) == 1 and b}
    once_new = {b for b in new_n if new_n.count(b) == 1 and b}
    shared = once_old & once_new
    if [b for b in old_n if b in shared] != [b for b in new_n if b in shared]:
        found.append("existing lines reordered")
    numbered_old = {_norm(b) for b in old_blocks if _NUM_RE.match(b)}
    if any(_NUM_RE.match(b) and _norm(b) in shared and _norm(b) not in numbered_old for b in new_blocks):
        found.append("steps newly numbered")
    if numbered_old:
        # A pure insertion (more new blocks than old ones in a diff hunk) of a numbered step
        # with an old numbered step after it. A reworded step is a same-size replace: not a hop.
        sm = difflib.SequenceMatcher(None, old_n, new_n, autojunk=False)
        for op, i1, i2, j1, j2 in sm.get_opcodes():
            if op in ("insert", "replace") and j2 - j1 > i2 - i1 \
                    and any(_NUM_RE.match(b) for b in new_blocks[j1:j2]) \
                    and any(_NUM_RE.match(b) for b in old_blocks[i2:]):
                found.append("step inserted into the numbered sequence")
                break
    return found


def classify(old_section: str, new_section: str, writer_kind: str | None = None) -> Check:
    """The checked kind of the edit old_section -> new_section (see the module doc)."""
    old_b, new_b = _blocks(old_section), _blocks(new_section)
    old_text, new_text = "\n".join(old_b), "\n".join(new_b)
    new = _new_chars(old_text, new_text)
    contract = _cues(CONTRACT_CUES, new_text, new)
    hop = _structure(old_b, new_b) + _cues(HOP_CUES, new_text, new)
    supported = [k for k, ev in (("contract", contract), ("hop", hop)) if ev]
    if writer_kind in supported:
        kind = writer_kind
    elif supported:
        kind = supported[0]
    else:
        kind = "prose"
    if kind == "contract":
        reason = "new or sharpened " + ", ".join(contract)
    elif kind == "hop":
        reason = "step order changed: " + ", ".join(hop)
    else:
        reason = "no step-order change and no new output or check clause"
    return Check(kind, reason, contract, hop)
