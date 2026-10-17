"""Evidence refs and deterministic ids, shared by every integration (blueprint C1).

A ref names one record in one system:

    amem:<entity>/<id>     an authoritative Supabase row       amem:session/<uuid>, amem:proposal/<uuid>
    ch:<table>/<key>       a ClickHouse row or named-query row  ch:deviations/<uuid>, ch:q.rule_gap/<rule>
    lf:<kind>/<id>         a Langfuse trace, score or dataset   lf:trace/<32 hex>
    past:<id>              a past.dev memory point              past:<source id>
    ncm:run/<id>           an NCM research run                  ncm:run/ncm-20261020-sudoku-easy

Supabase is the only place that maps an external ref to an authoritative row: evidence from another
system is used only after its `amem:` subject refs were re-read from Supabase (evidence.resolve).

Ids are deterministic, so a retry finds the same record in every system:
- run_key  = <experiment_id>/<arm>/<case>/<rep>
- trace id = the first 32 hex of sha256(run_key)        (Langfuse accepts a custom 32-hex trace id)
- session  = uuid5(NS_RSI, run_key)                     (passed to `claude --session-id`)
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass

# Fixed namespace for uuid5 ids. Never change it: it would orphan every existing session id.
NS_RSI = uuid.UUID("5b0f4c5e-2f43-5d6e-9a1b-7c3d2e1f0a9b")

# scheme -> rsi_evidence.system
SYSTEMS = {"amem": "supabase", "ch": "clickhouse", "lf": "langfuse", "past": "pastdev", "ncm": "ncm"}
SOURCES = {"sb": "amem", "ch": "ch", "lf": "lf", "pd": "past", "ncm": "ncm"}   # CLI --source -> scheme

# amem entity -> (table, key column). The only tables an amem: ref may point at.
AMEM_ENTITIES = {
    "session": ("sessions", "session_id"),
    "run": ("runs", "id"),
    "deviation": ("deviations", "id"),
    "score": ("scores", "id"),
    "edit": ("edits", "id"),
    "skill_call": ("skill_calls", "id"),
    "experiment": ("rsi_experiments", "id"),
    "candidate": ("rsi_candidates", "id"),
    "evidence": ("rsi_evidence", "id"),
    "artifact": ("rsi_artifacts", "hash"),
    "eval_result": ("rsi_eval_results", "id"),
    "proposal": ("rsi_proposals", "id"),
    "decision": ("rsi_decisions", "id"),
}

_ID = r"[A-Za-z0-9][A-Za-z0-9._:/@-]{0,199}"      # session ids hold ':' (codex:, cloud:) and '/' (agents)
_PATTERNS = {
    "amem": re.compile(rf"^amem:(?P<kind>[a-z_]+)/(?P<id>{_ID})$"),
    "ch": re.compile(rf"^ch:(?P<kind>[a-z][a-z0-9_.]{{0,63}})/(?P<id>{_ID})$"),
    "lf": re.compile(r"^lf:(?P<kind>trace|score|dataset|dataset_run|observation)/(?P<id>[A-Za-z0-9._-]{1,128})$"),
    "past": re.compile(rf"^past:(?P<id>{_ID})$"),
    "ncm": re.compile(r"^ncm:(?P<kind>run)/(?P<id>[A-Za-z0-9][A-Za-z0-9._-]{0,99})$"),
}
EXPERIMENT_RE = re.compile(r"^exp-\d{8}-[A-Za-z0-9][A-Za-z0-9-]{0,40}$")   # exp-YYYYMMDD-NN and exp-YYYYMMDD-edit-<hex>
TRACE_RE = re.compile(r"^[0-9a-f]{32}$")


class RefError(ValueError):
    pass


@dataclass(frozen=True)
class Ref:
    scheme: str
    kind: str          # amem entity, ch table, lf kind, "run" for ncm, "" for past
    id: str

    @property
    def system(self) -> str:
        return SYSTEMS[self.scheme]

    def __str__(self) -> str:
        return f"{self.scheme}:{self.id}" if self.scheme == "past" else f"{self.scheme}:{self.kind}/{self.id}"


def parse(text: str) -> Ref:
    if not isinstance(text, str):
        raise RefError("a ref is a string")
    scheme = text.split(":", 1)[0]
    pat = _PATTERNS.get(scheme)
    m = pat.fullmatch(text) if pat else None
    if m is None:
        raise RefError(f"not an evidence ref: {text[:80]!r}")
    kind = m.groupdict().get("kind") or ""
    if scheme == "amem" and kind not in AMEM_ENTITIES:
        raise RefError(f"unknown amem entity: {kind!r}")
    return Ref(scheme, kind, m.group("id"))


def is_ref(text: str) -> bool:
    try:
        parse(text)
        return True
    except RefError:
        return False


def _make(text: str) -> str:
    return str(parse(text))                  # validates on the way out too


def amem(entity: str, id_: str) -> str:
    return _make(f"amem:{entity}/{id_}")


def ch(table: str, key: str) -> str:
    return _make(f"ch:{table}/{key}")


def lf(kind: str, id_: str) -> str:
    return _make(f"lf:{kind}/{id_}")


def past(id_: str) -> str:
    return _make(f"past:{id_}")


def ncm_run(id_: str) -> str:
    return _make(f"ncm:run/{id_}")


def run_key(experiment_id: str, arm: str, case: str, rep: int) -> str:
    if not EXPERIMENT_RE.fullmatch(experiment_id):
        raise RefError(f"not an experiment id: {experiment_id!r}")
    if not re.fullmatch(r"[A-Z]", arm):
        raise RefError(f"arm is one capital letter: {arm!r}")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}", case) or not isinstance(rep, int) or rep < 0:
        raise RefError(f"bad case or rep: {case!r} {rep!r}")
    return f"{experiment_id}/{arm}/{case}/{rep}"


def trace_id(key: str) -> str:
    """Langfuse trace id for a run_key: 32 lowercase hex (W3C trace-id size), the same on every retry."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


def session_uuid(key: str) -> str:
    return str(uuid.uuid5(NS_RSI, key))
