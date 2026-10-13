"""`amem sessions tokens`: token totals per repo / tool / kind / model and the top sessions
(docs/sessions-live.md, "Report").

A session counts when its last activity (else its end, start or creation time) is in the
window. Subagents are their own rows (kind worker), so every total here counts each API
call once; a parent's `meta.subagent_tokens` is shown on the parent only and never added.
Rows without token columns (not measured yet) count as sessions, not as tokens.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg.rows import dict_row

from agent_memory import session_tokens, store

FIELDS = session_tokens.FIELDS
DEFAULT_DAYS = 7
TOP = 10

_SQL = """
select session_id, tool, kind, repo, started_at,
       coalesce(last_activity_at, ended_at, started_at, created_at) as active_at,
       tokens_input, tokens_output, tokens_cache_read, tokens_cache_creation, models, turns,
       meta -> 'subagent_tokens' as subagent_tokens
from sessions
where coalesce(last_activity_at, ended_at, started_at, created_at) >= %(since)s
  and (%(repo)s::text is null or repo = %(repo)s)
  and (%(tool)s::text is null or tool = %(tool)s)
"""


@dataclass
class Group:
    key: str
    sessions: int = 0
    tokens: session_tokens.Tokens = field(default_factory=session_tokens.Tokens)

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "sessions": self.sessions, **self.tokens.as_dict(), "total": self.tokens.total}


def fetch(*, days: int = DEFAULT_DAYS, repo: str | None = None, tool: str | None = None,
          now: datetime | None = None, db_url: str | None = None) -> list[dict[str, Any]]:
    since = (now or datetime.now(UTC)) - timedelta(days=days)
    with store.connect(db_url) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_SQL, {"since": since, "repo": repo, "tool": tool})
        return cur.fetchall()


def _tokens(rec: dict[str, Any]) -> session_tokens.Tokens | None:
    if rec.get("tokens_input") is None:
        return None
    return session_tokens.Tokens(*(int(rec.get(f"tokens_{f}") or 0) for f in FIELDS))


def summarize(rows: list[dict[str, Any]], *, days: int, repo: str | None = None,
              tool: str | None = None, top: int = TOP) -> dict[str, Any]:
    """The report as a dict (the --json output)."""
    total = Group("all")
    groups: dict[str, dict[str, Group]] = {"repo": {}, "tool": {}, "kind": {}, "model": {}}
    measured: list[tuple[int, dict[str, Any], session_tokens.Tokens]] = []

    def bump(dim: str, key: str, t: session_tokens.Tokens | None, sessions: int = 1) -> None:
        g = groups[dim].setdefault(key, Group(key))
        g.sessions += sessions
        if t is not None:
            g.tokens.add(t)

    for rec in rows:
        t = _tokens(rec)
        total.sessions += 1
        if t is not None:
            total.tokens.add(t)
            measured.append((t.total, rec, t))
        bump("repo", rec.get("repo") or "(none)", t)
        bump("tool", rec.get("tool") or "claude", t)
        bump("kind", rec.get("kind") or "?", t)
        models = rec.get("models") if isinstance(rec.get("models"), dict) else {}
        for name, v in models.items():
            if isinstance(v, dict):
                bump("model", str(name), session_tokens.Tokens.of(v))
    measured.sort(key=lambda x: (-x[0], str(x[1]["session_id"])))

    def ordered(dim: str) -> list[dict[str, Any]]:
        return [g.as_dict() for g in sorted(groups[dim].values(), key=lambda g: (-g.tokens.total, g.key))]

    def session(rec: dict[str, Any], t: session_tokens.Tokens) -> dict[str, Any]:
        sub = rec.get("subagent_tokens") if isinstance(rec.get("subagent_tokens"), dict) else None
        return {"session_id": rec["session_id"], "tool": rec["tool"], "kind": rec["kind"], "repo": rec["repo"],
                "started_at": _iso(rec.get("started_at")), "last_activity_at": _iso(rec.get("active_at")),
                "turns": rec.get("turns"), **t.as_dict(), "total": t.total, "subagent_tokens": sub}

    return {
        "days": days, "repo": repo, "tool": tool,
        "sessions": total.sessions, "measured": len(measured),
        "totals": {**total.tokens.as_dict(), "total": total.tokens.total},
        "by_repo": ordered("repo"), "by_tool": ordered("tool"), "by_kind": ordered("kind"),
        "by_model": ordered("model"),
        "top": [session(rec, t) for _n, rec, t in measured[:top]],
    }


def _iso(value: Any) -> str | None:
    return value.astimezone(UTC).isoformat() if isinstance(value, datetime) else None


def human(n: int | None) -> str:
    """1234 -> 1.2k, 45_100_000 -> 45.1M."""
    if n is None:
        return "-"
    for limit, unit in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "k")):
        if abs(n) >= limit:
            return f"{n / limit:.1f}{unit}"
    return str(n)


def short_id(sid: str) -> str:
    head, _, tail = sid.partition("/")
    prefix, _, rest = head.rpartition(":")
    head = (prefix + ":" if prefix else "") + rest[:8]
    return f"{head}/{tail[:16]}" if tail else head


def lines(doc: dict[str, Any]) -> list[str]:
    """The report as plain text, aligned for a monospace chat block."""
    t = doc["totals"]
    scope = [f"last {doc['days']} days"]
    if doc.get("repo"):
        scope.append(f"repo {doc['repo']}")
    if doc.get("tool"):
        scope.append(f"tool {doc['tool']}")
    out = [f"tokens: {', '.join(scope)}: {doc['sessions']} sessions ({doc['measured']} measured), "
           f"total {human(t['total'])}",
           f"  input {human(t['input'])}  output {human(t['output'])}  cache read {human(t['cache_read'])}  "
           f"cache write {human(t['cache_creation'])}"]
    if not doc["sessions"]:
        return out
    for dim in ("repo", "tool", "kind", "model"):
        items = doc[f"by_{dim}"]
        if not items:
            continue
        out += ["", f"by {dim}"]
        width = min(max(len(g["key"]) for g in items), 40)
        for g in items:
            count = f"{g['sessions']} sessions"
            out.append(f"  {g['key'][:40]:<{width}}  {count:>13}  total {human(g['total']):>7}  "
                       f"out {human(g['output']):>7}")
    if doc["top"]:
        out += ["", "top sessions"]
        for s in doc["top"]:
            when = (s["last_activity_at"] or "")[:16].replace("T", " ")
            turns = f"{s['turns']} turns" if s.get("turns") is not None else ""
            sub = session_tokens.Tokens.of(s.get("subagent_tokens"))
            subs = f"  (subagents {human(sub.total)})" if sub.total else ""
            out.append(f"  {when:<16}  {s['tool']}/{s['kind']:<8}  {(s['repo'] or '(none)')[:24]:<24}  "
                       f"{short_id(s['session_id']):<26}  total {human(s['total']):>7}  "
                       f"out {human(s['output']):>7}  {turns}{subs}".rstrip())
    return out
