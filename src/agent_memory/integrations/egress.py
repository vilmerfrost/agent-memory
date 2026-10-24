"""What may leave the Mac, through any sink: repo ownership and the egress deny list (Mission 004C, F3, F8).

Ownership (F8: "unknown repository ownership must default to private"). Every session is one of three classes,
decided only from what sessions_ingest stored in `sessions.meta` (never a git or network call):

    team      meta.team is true (sessions_ingest.team_info: the git remote owner is not a personal owner, a
              `mode: team` parent profile, or another session of the same repo in that ingest was team).
    own       meta.team is false AND meta.remote_owner (case-insensitive) is an owner account AND
              meta.remote_host (case-insensitive) is in OWN_HOSTS (github.com). Ingestion writes team = false
              only together with the remote owner and host it read, so this is positive knowledge: the repo's
              git remote is github.com/<one of the owner's accounts>/...
    private   everything else: no remote, not a git repo, a remote whose owner could not be read (a local path,
              a single-segment URL), a remote owner that is not an owner account, a remote on any other host
              (`gitlab.example.com/vilmerfrost/x`, an ssh alias such as `github-work`: an alias is not
              resolved), a session ingested before the team flag or the host was stored, or no session at all.

Owner accounts: AMEM_OWNER_ACCOUNTS (comma-separated), else AMEM_PERSONAL_OWNERS (the list ingestion decided
team = false from), else `vilmerfrost` (sessions_ingest.DEFAULT_PERSONAL_OWNERS). Each name must match
ACCOUNT_RE or it is dropped (fewer accounts only ever means more private). Because ingestion stores
team = false only for a personal owner, AMEM_OWNER_ACCOUNTS can narrow what counts as own but never widen it.

Rows stored before ingestion kept the host (Mission 004C) have team = false and an owner but no remote_host, so
they are private. A later ingest of the same transcript adds the host (the sessions upsert merges meta), and
`amem sessions hosts` (session_hosts.py) re-derives it from the repo's local git config (dry run unless
--apply; never the network). Until then their repo and project names are withheld names below (deny terms)
unless an own session has the same name.

Rows tied to a session take its class: a deviation (team also when its own meta says so), a skill call (no
matched session: private, F7). A row tied to repo names (a lesson's run repo and scope repo) is team when a team
session has that repo or project, own when every name is a repo or project of an own session, else private.
`classify` is the rule in Python, `class_sql` the same rule as SQL (with `bind_owners`).

The deny list. Two sources, joined at the start of every sync run:

    AMEM_EGRESS_DENY   comma-separated terms, set locally (never sent anywhere). Matched case-insensitively
                       as substrings: `acme` denies `acme-portal` and `Acme`.
    withheld names     every repo and project of a team session, and of a private session unless an own session
                       has the same name (then the name is the owner's own repo name too), read from Supabase by
                       the sink.

What a match does:
- ClickHouse: a label, pseudonym-source or id value that matches, or is email-shaped, ships as null; a row
  whose key matches is dropped (clickhouse.map_row).
- past.dev: a record whose raw text or final content matches is refused, never sent (sanitize_record).
- Langfuse: `langfuse.sensitive()` is true, so a model or tool name ships as `redacted` and a run whose
  identity (experiment id, case, run id) matches is refused.

A withheld name shorter than MIN_TEAM_TERM characters, and a generic placeholder that sessions_ingest.project_of
gives to work outside a repo (`unknown`, `evals`, `scratch`, `home`, and `routine:*`) or a mask label (`team`,
`private`), never becomes a term: those are not employer names, and as substrings they would null nearly every
label. Nor does an agent worktree's generated name (`agent-<hex id>`, AGENT_WORKTREE_RE): a subagent session that
ran in `<repo>/.claude/worktrees/agent-<id>` was stored with that folder as its repo (sessions_ingest.repo_of, fixed
2026-10-24), and as a term it is a substring of the session's own id `<parent>/agent-<id>`, so ClickHouse dropped
the row (sessions parity off by 8). An explicit AMEM_EGRESS_DENY term is always used as given.

Langfuse's `sensitive(text)` has no connection, so the Langfuse sink stores the names it read for this
process with `use_team_names` (replaced on every run); `current()` joins them with the env terms.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import psycopg

from agent_memory import config as amem_config

DENY_ENV = "AMEM_EGRESS_DENY"
MIN_TEAM_TERM = 3
GENERIC_NAMES = frozenset({"unknown", "evals", "scratch", "home", "team", "private", "global"})
AGENT_WORKTREE_RE = re.compile(r"agent-[0-9a-f]{8,40}")      # a Claude Code agent's worktree folder: generated
EMAIL_RE = re.compile(r"[^@\s/]+@[^@\s/]+\.[A-Za-z]{2,}")

# ---------------------------------------------------------------- ownership (F8)

OWN, TEAM, PRIVATE = "own", "team", "private"
CLASSES = (OWN, TEAM, PRIVATE)
MASKS = (TEAM, PRIVATE)
OWNER_ACCOUNTS_ENV = "AMEM_OWNER_ACCOUNTS"
PERSONAL_OWNERS_ENV = "AMEM_PERSONAL_OWNERS"          # = sessions_ingest.PERSONAL_OWNERS_ENV (a test pins it)
DEFAULT_OWNER_ACCOUNTS = ("vilmerfrost",)             # = sessions_ingest.DEFAULT_PERSONAL_OWNERS
ACCOUNT_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,99}")  # lowercase; never a quote, so safe as a SQL literal
OWNERS_TOKEN = "__amem_owner_accounts__"              # class_sql's placeholder; unbound SQL fails (no such column)
OWN_HOSTS = ("github.com",)                           # the only remote host an own repo may live on
if not all(ACCOUNT_RE.fullmatch(h) for h in OWN_HOSTS):  # written into SQL as literals: never a quote
    raise ValueError("OWN_HOSTS must be plain lowercase host names")
_HOSTS_SQL = "array[" + ", ".join(f"'{h}'" for h in OWN_HOSTS) + "]::text[]"


def owner_accounts(environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """The owner's accounts, lowercase and sorted: AMEM_OWNER_ACCOUNTS, else AMEM_PERSONAL_OWNERS, else the
    default. A name that does not match ACCOUNT_RE is dropped."""
    if environ is None:
        amem_config.load_env()
        environ = os.environ
    raw = (environ.get(OWNER_ACCOUNTS_ENV) or "").strip() or (environ.get(PERSONAL_OWNERS_ENV) or "").strip()
    names = raw.split(",") if raw else DEFAULT_OWNER_ACCOUNTS
    return tuple(sorted({n.strip().lower() for n in names if ACCOUNT_RE.fullmatch(n.strip().lower())}))


def _flag(v: Any) -> str | None:
    """A jsonb value as `->>` gives it: true -> 'true', false -> 'false', a string as is."""
    if isinstance(v, bool):
        return "true" if v else "false"
    return None if v is None else str(v)


def classify(meta: Any, owners: Iterable[str] | None = None) -> str:
    """own | team | private for one session's stored meta (the rule in the module docstring)."""
    m = meta if isinstance(meta, Mapping) else {}
    if _flag(m.get("team")) == "true":
        return TEAM
    accounts = {a.strip().lower() for a in (owner_accounts() if owners is None else owners)}
    owner, host = m.get("remote_owner"), m.get("remote_host")
    if _flag(m.get("team")) == "false" and isinstance(owner, str) and owner.strip().lower() in accounts \
            and isinstance(host, str) and host.strip().lower() in OWN_HOSTS:
        return OWN
    return PRIVATE


def class_sql(meta: str) -> str:
    """The same rule as SQL text over a sessions.meta jsonb expression: 'team' | 'own' | 'private'. The owner
    list is the OWNERS_TOKEN placeholder: run the text through bind_owners before executing it."""
    return (f"(case when ({meta}) ->> 'team' = 'true' then 'team' "
            f"when ({meta}) ->> 'team' = 'false' and lower(btrim(({meta}) ->> 'remote_owner')) = any({OWNERS_TOKEN}) "
            f"and lower(btrim(({meta}) ->> 'remote_host')) = any({_HOSTS_SQL}) "
            f"then 'own' else 'private' end)")


def bind_owners(text: str, owners: Iterable[str]) -> str:
    """`text` with OWNERS_TOKEN replaced by a text[] literal of the owner accounts. Only ACCOUNT_RE names are
    written (no quote can reach the SQL); none left: an empty array, so nothing is own."""
    names = sorted({m for m in (n.strip().lower() for n in owners) if ACCOUNT_RE.fullmatch(m)})
    return text.replace(OWNERS_TOKEN, "array[" + ", ".join(f"'{n}'" for n in names) + "]::text[]")


# ---------------------------------------------------------------- the deny list

TEAM_NAMES_SQL = f"""
with c as (select s.repo, s.project, {class_sql('s.meta')} as cls from sessions s),
n as (select x.v, c.cls from c cross join lateral (values (c.repo), (c.project)) as x(v) where x.v is not null)
select distinct v from n
where cls = 'team' or (cls = 'private' and v not in (select v from n where cls = 'own'))
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
        if len(t) >= MIN_TEAM_TERM and t not in GENERIC_NAMES and not t.startswith("routine:") \
                and not AGENT_WORKTREE_RE.fullmatch(t):
            out.add(t)
    return Deny(tuple(sorted(out)))


def team_names(conn: psycopg.Connection, environ: Mapping[str, str] | None = None, *,
               owners: Iterable[str] | None = None) -> Deny:
    """Every withheld repo and project name (team, and private unless an own session has the name), as deny
    terms (a read; works on a read-only connection). `owners`: the owner accounts (default: from `environ`, else
    the process env)."""
    q = bind_owners(TEAM_NAMES_SQL, owner_accounts(environ) if owners is None else owners)
    return team_terms(r[0] for r in conn.execute(q).fetchall())


def load(conn: psycopg.Connection, environ: Mapping[str, str] | None = None) -> Deny:
    """The deny list for one sync run: the env terms and the withheld names in Supabase now."""
    return env_terms(environ) | team_names(conn, environ)


_team = NONE


def use_team_names(deny: Deny) -> None:
    """Remember the team names a sink read for this run (for checks that have no connection: Langfuse)."""
    global _team
    _team = deny


def current(environ: Mapping[str, str] | None = None) -> Deny:
    """The env terms now, plus the team names the last sink run remembered."""
    return env_terms(environ) | _team
