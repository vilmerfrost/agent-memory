"""`amem sessions hosts`: re-derive `meta.remote_host` for sessions stored before ingestion kept it (Mission 004C).

Ingestion now stores the git remote's host next to its owner (sessions_ingest.team_info), and egress.py counts a
session as own only when that host is github.com. A row stored before has `team: false` and `remote_owner` but no
`remote_host`, so it is private: ClickHouse masks it and past.dev never receives a record tied to it. A later
ingest of the same transcript adds the host (the sessions upsert merges meta), but only for transcripts inside the
ingest window whose repo still exists.

This command restores the host for the other rows from local files only (no git process, no network):

    candidates  team = false, remote_owner is an owner account (egress.owner_accounts), no remote_host
    paths       meta.cwd, then every meta.cwds entry, in order
    remote      sessions_ingest.git_remote_url(path): the repo's .git/config (a worktree: its main checkout)

The first path whose remote has a readable owner and host decides the row's outcome:
    rederived      the remote's owner equals the stored remote_owner (case-insensitive): its host is written
                   (own when it is github.com, private otherwise)
    owner-changed  the remote names another owner today: nothing is written (the row stays private)
    no-remote      no path has such a remote (the worktree was removed, the repo moved or was deleted):
                   nothing is written (the row stays private)

Dry run unless --apply; the dry run reads the database in a read-only transaction. --apply writes `remote_host`
and `remote_host_source: "rederived"` into meta, only while meta still has no remote_host. --revert removes both
keys from every row whose remote_host_source is `rederived`.

Trade-off: the host is the one the repo's remote names today, not at session time. A repo whose remote moved
from another host to github.com (same owner) since the session counts as own for that older work too. The owner
check limits this to the same account; a moved or deleted repo stays private (the safe direction).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from psycopg.types.json import Jsonb

from agent_memory import sessions_ingest, store
from agent_memory.integrations import egress

SOURCE = "rederived"
REDERIVED, OWNER_CHANGED, NO_REMOTE = "rederived", "owner-changed", "no-remote"
OUTCOMES = (REDERIVED, OWNER_CHANGED, NO_REMOTE)

_CANDIDATES_SQL = f"""
select session_id, meta ->> 'remote_owner', meta -> 'cwd', meta -> 'cwds'
from sessions
where meta ->> 'team' = 'false' and lower(btrim(meta ->> 'remote_owner')) = any({egress.OWNERS_TOKEN})
  and meta ->> 'remote_host' is null
order by session_id
"""


@dataclass(frozen=True)
class Row:
    session_id: str
    outcome: str              # rederived | owner-changed | no-remote
    host: str | None = None   # rederived only


@dataclass
class Plan:
    rows: list[Row] = field(default_factory=list)
    owners: tuple[str, ...] = ()

    def counts(self) -> dict[str, int]:
        c = Counter(r.outcome for r in self.rows)
        out = {"candidates": len(self.rows), **{k: c.get(k, 0) for k in OUTCOMES}}
        out["own_after"] = sum(1 for r in self.rows if r.outcome == REDERIVED and r.host in egress.OWN_HOSTS)
        return out

    def to_json(self) -> dict[str, Any]:
        return {"owner_accounts": list(self.owners), "own_hosts": list(egress.OWN_HOSTS), "counts": self.counts(),
                "rows": [{"session_id": r.session_id, "outcome": r.outcome, "host": r.host} for r in self.rows]}

    def line(self) -> str:
        n = self.counts()
        return (f"sessions hosts: {n['candidates']} without a host: {n[REDERIVED]} rederived ({n['own_after']} on "
                f"{', '.join(egress.OWN_HOSTS)}: own after --apply), {n[OWNER_CHANGED]} owner-changed, "
                f"{n[NO_REMOTE]} no-remote (both stay private)")


def _paths(cwd: Any, cwds: Any) -> list[str]:
    out: list[str] = []
    for p in [cwd, *(cwds if isinstance(cwds, list) else [])]:
        if isinstance(p, str) and p.strip() and p not in out:
            out.append(p)
    return out


def derive(owner: str, paths: Iterable[str]) -> Row:
    """The outcome for one row (session id left empty): the first path whose remote has an owner and a host."""
    for p in paths:
        url = sessions_ingest.git_remote_url(p)
        got, host = sessions_ingest.remote_owner(url), sessions_ingest.remote_host(url)
        if not got or not host:
            continue
        if got.strip().lower() != owner.strip().lower():
            return Row("", OWNER_CHANGED)
        return Row("", REDERIVED, host)
    return Row("", NO_REMOTE)


def plan(*, db_url: str | None = None, owners: Iterable[str] | None = None) -> Plan:
    """Every candidate row and its outcome. Reads the database in a read-only transaction; writes nothing."""
    accounts = egress.owner_accounts() if owners is None else tuple(owners)
    with store.connect(db_url) as conn:
        conn.read_only = True
        try:
            rows = conn.execute(egress.bind_owners(_CANDIDATES_SQL, accounts)).fetchall()
        finally:
            conn.rollback()
    out = Plan(owners=tuple(sorted({a.strip().lower() for a in accounts})))
    for sid, owner, cwd, cwds in rows:
        r = derive(owner or "", _paths(cwd, cwds))
        out.rows.append(Row(sid, r.outcome, r.host))
    return out


def apply(p: Plan, *, db_url: str | None = None) -> int:
    """Write the rederived hosts (one transaction). Returns the rows written; a row that gained a host since the
    plan is left alone."""
    n = 0
    with store.connect(db_url) as conn:
        for r in p.rows:
            if r.outcome != REDERIVED or not r.host:
                continue
            cur = conn.execute("update sessions set meta = meta || %s where session_id = %s "
                               "and meta ->> 'remote_host' is null",
                               (Jsonb({"remote_host": r.host, "remote_host_source": SOURCE}), r.session_id))
            n += cur.rowcount
    return n


def revert(*, db_url: str | None = None) -> int:
    """Remove every host this command wrote (remote_host_source = rederived). Returns the rows changed."""
    with store.connect(db_url) as conn:
        cur = conn.execute("update sessions set meta = meta - 'remote_host' - 'remote_host_source' "
                           "where meta ->> 'remote_host_source' = %s", (SOURCE,))
        return cur.rowcount
