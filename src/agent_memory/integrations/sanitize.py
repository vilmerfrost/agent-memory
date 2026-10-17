"""The sanitizer for decision records that leave for past.dev (Mission 004A; blueprint C3, the past.dev contract).

past.dev is an external SaaS. It receives only short decision records that we build ourselves:

    kind: proposal | experiment | decision | rejection | edit | lesson
    ref: amem:<entity>/<id>          our own ref; the only id that may leave
    rule: <source>/<rule_id>
    hypothesis, change_kind, result, decision, reason, night

`sanitize_record(record) -> (content | None, reasons)` turns one record into the exact text that is sent,
or refuses it. Every text field goes through, in this order:

1. Unicode NFKC, control and format characters removed (no zero-width or full-width tricks).
2. `agent_memory.redact.redact` (the one credential redaction for everything that leaves this machine).
3. Secret shapes that redact does not know (past.dev, Stripe, webhook, npm and Hugging Face keys, PEM
   headers, `password=...` with short values, bare bearer values, long mixed-case base64): the record is
   refused, never scrubbed.
4. Scrubbers: URLs, emails, Windows and UNC paths, home paths (`~/`, `$HOME`, `%USERPROFILE%`), absolute
   paths, `Users/...`-style paths without the slash, dot-folder paths, relative paths that end in a file
   name, IPv4/IPv6 addresses, `localhost`, host names (a known TLD, or three or more labels; never a file
   extension such as `.md`), UUIDs, hex ids (7+ hex with a digit and a letter) and long word tokens
   (20+ characters with a digit and a letter). The record's own ref id is kept.
5. Whitespace collapsed, each field cut to its cap.

The record is refused (`None`) when it is a team item (`team` or `meta.team` true), a private item (`private`
true: tied to a repo or session whose ownership is not positively the owner's, Mission 004C F8, egress.py), it
names a term on the egress deny list (Mission 004C; raw fields or final text), its kind or ref is not one we send, a secret
shape survives (checked before and after scrubbing, on the final text too), the result is not stable under
a second pass, or the final text exceeds CAP_BYTES. The same record always gives the same bytes.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from typing import Any

from agent_memory import redact
from agent_memory.integrations import egress, refs

CAP_BYTES = 1536                                   # ~1.5 KB per record, after trimming
FIELDS = ("kind", "ref", "rule", "hypothesis", "change_kind", "result", "decision", "reason", "night")
TEXT_FIELDS = ("rule", "hypothesis", "change_kind", "result", "decision", "reason")
FIELD_CAPS = {"rule": 160, "hypothesis": 500, "change_kind": 40, "result": 240, "decision": 40, "reason": 500}
KIND_ENTITY = {"proposal": "proposal", "experiment": "experiment", "decision": "decision", "rejection": "decision",
               "edit": "edit", "lesson": "run"}
KINDS = tuple(KIND_ENTITY)
NIGHT_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
# A ref id that may leave: a UUID, or a lowercase slug such as an experiment id. Nothing with '@', '.', ':'.
SAFE_REF_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[a-z0-9][a-z0-9-]{2,60}")

# Secret shapes that redact.redact does not replace. A match refuses the whole record.
SECRET_RES = (
    ("past-key", re.compile(r"(?i)past_(?:sk|mk)_[A-Za-z0-9_-]{4,}")),
    ("stripe-key", re.compile(r"(?i)\b(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{4,}")),
    ("webhook-secret", re.compile(r"\bwhsec_[A-Za-z0-9]{8,}")),
    ("npm-token", re.compile(r"\bnpm_[A-Za-z0-9]{20,}")),
    ("hf-token", re.compile(r"\bhf_[A-Za-z0-9]{20,}")),
    ("pem", re.compile(r"-----BEGIN [A-Z0-9 ]*-----")),
    ("assignment", re.compile(r"(?i)\b(?:password|passwd|pwd|secret|api[_-]?key|token|private[_-]?key|client[_-]?secret)"
                              r"\s*[:=]\s*(?!\[redacted\])(?=\S*[\d!@#$%^&*])\S{6,}")),
    ("bearer", re.compile(r"(?i)\bbearer\s+(?!\[redacted\])[A-Za-z0-9._~+/=-]{20,}")),
    ("high-entropy", re.compile(r"(?<![A-Za-z0-9+/=])(?=[A-Za-z0-9+/=]*[A-Z])(?=[A-Za-z0-9+/=]*[a-z])"
                                r"(?=[A-Za-z0-9+/=]*\d)[A-Za-z0-9+/=]{32,}")),
)

# Host names: a label chain whose last label is a known TLD (or three or more labels), unless that last
# label is a file extension (CLAUDE.md, sync.py and Node.js stay).
HOST_TLDS = frozenset((
    "com net org io dev ai app co me us uk de se no dk fi fr nl eu ch at be es it ca au jp cn ru in info biz xyz "
    "cloud site online tech page run tv gg ly to cc ws live link services systems gov edu mil int onion "
    "local localhost localdomain internal intranet lan home corp test example invalid arpa").split())
FILE_EXTS = frozenset((
    "md mdx py pyi sh zsh bash fish json jsonl yaml yml toml txt js mjs cjs ts tsx jsx sql lock cfg ini conf rs go "
    "html htm css scss csv tsv log pen plist rb java kt swift c h cpp hpp cs php pl so dylib env pem key crt xml "
    "ipynb png jpg jpeg gif svg pdf docx pptx xlsx tmp bak diff patch gz zip tar tgz mp3 mp4 wav mov webm").split())


def _host(m: re.Match[str]) -> str:
    tld = m.group(2).lower()
    if tld in FILE_EXTS:
        return m.group(0)
    labels = m.group(1).count(".") + 1
    return "[host]" if tld in HOST_TLDS or labels >= 3 else m.group(0)


_P = r"[^\s\"'<>|()\[\]{},;`]"                     # one path character
_SCRUBBERS: tuple[tuple[str, re.Pattern[str], Any], ...] = (
    ("url", re.compile(r"(?i)\b[a-z][a-z0-9+.-]{1,15}://[^\s<>\"'`]*"), "[url]"),
    ("email", re.compile(r"[\w.%+-]+@[\w-]+(?:\.[\w-]+)*"), "[email]"),
    ("path", re.compile(r"(?i)(?<![\w])[a-z]:[\\/]" + _P + r"*"), "[path]"),                     # C:\x, C:/x
    ("path", re.compile(r"\\\\[^\s\\]+\\" + _P + r"*"), "[path]"),                               # \\server\share
    ("path", re.compile(r"(?<![\w~])~[\w.-]*/" + _P + r"*"), "[path]"),                          # ~/x, ~user/x
    ("path", re.compile(r"(?i)(?:\$HOME|\$\{HOME\}|%USERPROFILE%|%HOMEPATH%)" + _P + r"*"), "[path]"),
    ("path", re.compile(r"(?<![\w.~-])/(?=[\w.~-])" + _P + r"*"), "[path]"),                     # /Users/x, /tmp
    ("path", re.compile(r"(?i)(?<![\w.-])(?:Users|home|private|var|tmp|Volumes|opt|etc|root)/" + _P + r"*"), "[path]"),
    ("path", re.compile(r"(?<![\w.-])\.[\w-]+/" + _P + r"*"), "[path]"),                         # .claude/x
    ("path", re.compile(r"(?<![\w.-])[\w.-]+(?:/[\w.-]+)+\.[A-Za-z0-9]{1,8}\b"), "[path]"),      # src/x.py
    ("ip", re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?(?!\d)"), "[ip]"),
    ("ip", re.compile(r"(?i)(?<![\w:])(?:(?=[0-9a-f:]*[a-f])|(?=[0-9a-f:]*::))(?:[0-9a-f]{0,4}:){2,7}[0-9a-f]{0,4}"
                      r"(?![\w:])"), "[ip]"),
    ("host", re.compile(r"(?i)\blocalhost(?::\d{1,5})?\b"), "[host]"),
    ("host", re.compile(r"(?i)(?<![\w.-])((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+([a-z][a-z0-9-]{0,22}[a-z0-9]))"
                        r"(?::\d{1,5})?(?![\w-])"), _host),
    ("uuid", re.compile(r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"), "[id]"),
    ("hex", re.compile(r"(?i)\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,}\b"), "[id]"),
    ("token", re.compile(r"\b(?=\w*\d)(?=\w*[A-Za-z])\w{20,}\b"), "[id]"),
)
_OWN = "\x00own\x00"                               # placeholder for the record's own id (control chars are stripped first)
_WS = re.compile(r"\s+")


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    return "".join(" " if unicodedata.category(c) in ("Cc", "Zl", "Zp") else c
                   for c in text if unicodedata.category(c) != "Cf")


def secret_like(text: str) -> str | None:
    """The name of the first secret shape in `text` (after redact), or None."""
    if redact.redact(text) != text:
        return "credential"
    for name, pat in SECRET_RES:
        if pat.search(text):
            return name
    return None


def scrub_text(text: str, own_id: str | None = None) -> tuple[str, list[str]]:
    """Redact and scrub one text. Returns (text, what was scrubbed). Does not refuse; see sanitize_record."""
    found: list[str] = []
    text = _normalize(text)
    red = redact.redact(text)
    if red != text:
        found.append("redacted")
    text = red
    if own_id:
        text = re.sub(re.escape(own_id), _OWN, text, flags=re.I)
    for name, pat, repl in _SCRUBBERS:
        new = pat.sub(repl, text)
        if new != text:
            found.append(name)
            text = new
    if own_id:
        text = text.replace(_OWN, own_id)
    return _WS.sub(" ", text).strip(), found


def _cut(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    cut = text[: cap - 3]
    open_ = cut.rfind("[")
    if open_ > cap - 16 and "]" not in cut[open_:]:     # never leave half a placeholder like "[pa"
        cut = cut[:open_]
    return cut.rstrip() + "..."


def _team(record: Mapping[str, Any]) -> bool:
    meta = record.get("meta")
    return record.get("team") is True or (isinstance(meta, Mapping) and meta.get("team") is True)


def _text(value: Any) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def sanitize_record(record: Mapping[str, Any], deny: egress.Deny = egress.NONE) -> tuple[str | None, list[str]]:
    """(content, reasons): the exact text past.dev may receive, or (None, why it was refused).

    `deny` (Mission 004C, egress.py): a record whose raw text fields or final content name a denied term
    (AMEM_EGRESS_DENY, a team repo or project) is refused, never scrubbed."""
    if _team(record):
        return None, ["team"]
    if record.get("private") is True:                # Mission 004C (F8): tied to a repo of unknown ownership
        return None, ["private"]
    if any(deny.match(record.get(f)) for f in (*TEXT_FIELDS, "ref")):
        return None, ["deny"]
    kind = record.get("kind")
    if kind not in KIND_ENTITY:
        return None, ["kind"]
    ref = _text(record.get("ref"))
    try:
        r = refs.parse(ref)
    except refs.RefError:
        return None, ["ref"]
    if r.scheme != "amem" or r.kind != KIND_ENTITY[kind] or not SAFE_REF_ID.fullmatch(r.id):
        return None, ["ref"]
    reasons: set[str] = set()
    out: dict[str, str] = {"kind": kind, "ref": ref}
    for f in TEXT_FIELDS:
        raw = _text(record.get(f))
        if not raw.strip():
            continue
        hit = secret_like(redact.redact(_normalize(raw)))
        if hit:
            return None, [f"secret:{hit}:{f}"]
        clean, found = scrub_text(raw, r.id)
        reasons.update(f"{x}:{f}" for x in found)
        cut = _cut(clean, FIELD_CAPS[f])
        if cut != clean:
            reasons.add(f"trimmed:{f}")
        if scrub_text(cut, r.id)[0] != cut:           # a second pass must change nothing
            return None, [f"unstable:{f}"]
        if cut:
            out[f] = cut
    night = _text(record.get("night")).strip()
    if NIGHT_RE.fullmatch(night):
        out["night"] = night
    elif night:
        reasons.add("dropped:night")
    content = "\n".join(f"{k}: {out[k]}" for k in FIELDS if k in out)
    hit = secret_like(content)
    if hit:
        return None, [f"secret:{hit}:content"]
    if deny.match(content):
        return None, ["deny"]
    if len(content.encode("utf-8")) > CAP_BYTES:
        return None, ["cap"]
    return content, sorted(reasons)
