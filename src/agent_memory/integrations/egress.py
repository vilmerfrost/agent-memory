"""The egress deny list: names that must never leave the Mac, through any sink (Mission 004C, F3).

Two sources, joined at the start of every sync run:

    AMEM_EGRESS_DENY   comma-separated terms, set locally (never sent anywhere). Matched case-insensitively
                       as substrings: `acme` denies `acme-portal` and `Acme`.
    team names         every repo and project of a session with meta.team = true (an employer work repo,
                       flagged by sessions_ingest), read from Supabase by the sink.

What a match does:
- ClickHouse: a label, pseudonym-source or id value that matches, or is email-shaped, ships as null; a row
  whose key matches is dropped (clickhouse.map_row).
- past.dev: a record whose raw text or final content matches is refused, never sent (sanitize_record).
- Langfuse: `langfuse.sensitive()` is true, so a model or tool name ships as `redacted` and a run whose
  identity (experiment id, case, run id) matches is refused.

A team name shorter than MIN_TEAM_TERM characters, and a generic placeholder that sessions_ingest.project_of
gives to work outside a repo (`unknown`, `evals`, `scratch`, `home`, and `routine:*`), never becomes a term:
those are not employer names, and as substrings they would null nearly every label. An explicit
AMEM_EGRESS_DENY term is always used as given.

Langfuse's `sensitive(text)` has no connection, so the Langfuse sink stores the team names it read for this
process with `use_team_names` (replaced on every run); `current()` joins them with the env terms.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import psycopg

DENY_ENV = "AMEM_EGRESS_DENY"
MIN_TEAM_TERM = 3
GENERIC_NAMES = frozenset({"unknown", "evals", "scratch", "home", "team", "global"})
EMAIL_RE = re.compile(r"[^@\s/]+@[^@\s/]+\.[A-Za-z]{2,}")

TEAM_NAMES_SQL = """
select distinct v from sessions s cross join lateral (values (s.repo), (s.project)) as x(v)
where s.meta ->> 'team' = 'true' and v is not null
"""


@dataclass(frozen=True)
class Deny:
    terms: tuple[str, ...] = ()          # lowercase, sorted, unique

    def match(self, value: Any) -> bool:
        if value is None or not self.terms:
            return False
        s = str(value).lower()
        return any(t in s for t in self.terms)

    def __or__(self, other: Deny) -> Deny:
        return Deny(tuple(sorted(set(self.terms) | set(other.terms))))


NONE = Deny()


def email_like(value: Any) -> bool:
    return value is not None and EMAIL_RE.search(str(value)) is not None


def env_terms(environ: Mapping[str, str] | None = None) -> Deny:
    env = os.environ if environ is None else environ
    terms = {t.strip().lower() for t in (env.get(DENY_ENV) or "").split(",")}
    return Deny(tuple(sorted(t for t in terms if t)))


def team_terms(names: Iterable[Any]) -> Deny:
    out = set()
    for n in names:
        t = str(n or "").strip().lower()
        if len(t) >= MIN_TEAM_TERM and t not in GENERIC_NAMES and not t.startswith("routine:"):
            out.add(t)
    return Deny(tuple(sorted(out)))


def team_names(conn: psycopg.Connection) -> Deny:
    """Every repo and project of a team session, as deny terms (a read; works on a read-only connection)."""
    return team_terms(r[0] for r in conn.execute(TEAM_NAMES_SQL).fetchall())


def load(conn: psycopg.Connection, environ: Mapping[str, str] | None = None) -> Deny:
    """The deny list for one sync run: the env terms and the team names in Supabase now."""
    return env_terms(environ) | team_names(conn)


_team = NONE


def use_team_names(deny: Deny) -> None:
    """Remember the team names a sink read for this run (for checks that have no connection: Langfuse)."""
    global _team
    _team = deny


def current(environ: Mapping[str, str] | None = None) -> Deny:
    """The env terms now, plus the team names the last sink run remembered."""
    return env_terms(environ) | _team
