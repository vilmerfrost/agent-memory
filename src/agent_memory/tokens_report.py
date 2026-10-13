"""`amem sessions tokens`: token totals per category / project (default), repo, tool, kind,
model, effort or outcome, with active time, and the top sessions (docs/sessions-live.md, "Report").

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

DIMS = ("category", "project", "repo", "tool", "kind", "model", "effort", "outcome")
DEFAULT_BY = ("category", "model")   # category sections list their projects

_SQL = """
select session_id, tool, kind, repo, started_at, title, category, project, model, model_last, effort,
       outcome, duration_seconds, active_seconds,
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
    durations: list[float] = field(default_factory=list)
    actives: list[float] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "sessions": self.sessions, **self.tokens.as_dict(), "total": self.tokens.total,
                "duration_seconds": round(sum(self.durations), 3) if self.durations else None,
                "duration_median_seconds": median(self.durations) if self.durations else None,
                "active_seconds": round(sum(self.actives), 3) if self.actives else None,
                "active_median_seconds": median(self.actives) if self.actives else None}


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


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _key(rec: dict[str, Any], dim: str) -> str:
    defaults = {"tool": "claude", "kind": "?"}
    return str(rec.get(dim) or defaults.get(dim, "(none)"))


def summarize(rows: list[dict[str, Any]], *, days: int, repo: str | None = None,
              tool: str | None = None, top: int = TOP) -> dict[str, Any]:
    """The report as a dict (the --json output). `by_model` splits each session's tokens by
    model (the `models` breakdown); every other `by_<dim>` counts a session under its own value."""
    total = Group("all")
    groups: dict[str, dict[str, Group]] = {d: {} for d in DIMS}
    nested: dict[str, dict[str, Group]] = {}   # category -> project -> group
    measured: list[tuple[int, dict[str, Any], session_tokens.Tokens]] = []

    def bump(g: Group, t: session_tokens.Tokens | None, rec: dict[str, Any] | None, sessions: int = 1) -> None:
        g.sessions += sessions
        if t is not None:
            g.tokens.add(t)
        if rec is not None:
            if (d := _num(rec.get("duration_seconds"))) is not None:
                g.durations.append(d)
            if (a := _num(rec.get("active_seconds"))) is not None:
                g.actives.append(a)

    for rec in rows:
        t = _tokens(rec)
        bump(total, t, rec)
        if t is not None:
            measured.append((t.total, rec, t))
        for dim in DIMS:
            if dim == "model":
                continue
            key = _key(rec, dim)
            bump(groups[dim].setdefault(key, Group(key)), t, rec)
        cat, proj = _key(rec, "category"), _key(rec, "project")
        bump(nested.setdefault(cat, {}).setdefault(proj, Group(proj)), t, rec)
        models = rec.get("models") if isinstance(rec.get("models"), dict) else {}
        for name, v in models.items():
            if isinstance(v, dict):
                g = groups["model"].setdefault(str(name), Group(str(name)))
                bump(g, session_tokens.Tokens.of(v), None)
    measured.sort(key=lambda x: (-x[0], str(x[1]["session_id"])))

    def ordered(gs: dict[str, Group]) -> list[dict[str, Any]]:
        return [g.as_dict() for g in sorted(gs.values(), key=lambda g: (-g.tokens.total, -g.sessions, g.key))]

    def session(rec: dict[str, Any], t: session_tokens.Tokens) -> dict[str, Any]:
        sub = rec.get("subagent_tokens") if isinstance(rec.get("subagent_tokens"), dict) else None
        return {"session_id": rec["session_id"], "tool": rec["tool"], "kind": rec["kind"], "repo": rec["repo"],
                "title": rec.get("title"), "category": rec.get("category"), "project": rec.get("project"),
                "model": rec.get("model"), "model_last": rec.get("model_last"), "effort": rec.get("effort"),
                "outcome": rec.get("outcome"), "duration_seconds": _num(rec.get("duration_seconds")),
                "active_seconds": _num(rec.get("active_seconds")),
                "started_at": _iso(rec.get("started_at")), "last_activity_at": _iso(rec.get("active_at")),
                "turns": rec.get("turns"), **t.as_dict(), "total": t.total, "subagent_tokens": sub}

    by_category = ordered(groups["category"])
    for c in by_category:
        c["projects"] = ordered(nested.get(c["key"], {}))
    return {
        "days": days, "repo": repo, "tool": tool,
        "sessions": total.sessions, "measured": len(measured),
        "totals": {**total.tokens.as_dict(), "total": total.tokens.total},
        "duration_seconds": total.as_dict()["duration_seconds"],
        "active_seconds": total.as_dict()["active_seconds"],
        **{f"by_{d}": ordered(groups[d]) for d in DIMS if d != "category"},
        "by_category": by_category,
        "top": [session(rec, t) for _n, rec, t in measured[:top]],
    }


# --------------------------------------------------------------------------- `amem sessions cost`
# docs/sessions-live.md, "Cost of a window". The JSON shape is a contract: agent-config's
# `improve` reads it to show the cost of each kept change. Keep its keys.

_COST_SQL = """
select coalesce(project, 'unknown') as project, tokens_input, tokens_output, tokens_cache_read,
       tokens_cache_creation, active_seconds, api_seconds, tool_seconds
from sessions
where coalesce(started_at, created_at) >= %(since)s and coalesce(started_at, created_at) < %(until)s
  and (%(category)s::text is null or category = %(category)s)
  and (%(project)s::text is null or project = %(project)s)
"""


def _cost_bucket() -> dict[str, Any]:
    return {"sessions": 0, "tokens": {**{f: 0 for f in FIELDS}, "total": 0},
            "active_seconds": 0.0, "api_seconds": 0.0, "tool_seconds": 0.0}


def _cost_add(b: dict[str, Any], rec: dict[str, Any]) -> None:
    b["sessions"] += 1
    for f in FIELDS:
        n = int(rec.get(f"tokens_{f}") or 0)
        b["tokens"][f] += n
        b["tokens"]["total"] += n
    for k in ("active_seconds", "api_seconds", "tool_seconds"):
        b[k] = round(b[k] + float(rec.get(k) or 0), 3)


def cost(*, since: datetime, until: datetime | None = None, category: str | None = None,
         project: str | None = None, now: datetime | None = None, db_url: str | None = None) -> dict[str, Any]:
    """Totals of the sessions that started in [since, until) (until: now), each row once
    (subagents are their own rows; a parent's meta.subagent_tokens is never added).

    {"sessions": n, "tokens": {"input", "output", "cache_read", "cache_creation", "total"},
     "active_seconds", "api_seconds", "tool_seconds",
     "by_project": {project: the same keys without by_project}}
    Nothing matches: zeros and an empty by_project. Unmeasured columns count as 0."""
    until = until or now or datetime.now(UTC)
    with store.connect(db_url) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_COST_SQL, {"since": since, "until": until, "category": category, "project": project})
        rows = cur.fetchall()
    total = _cost_bucket()
    by_project: dict[str, dict[str, Any]] = {}
    for rec in rows:
        _cost_add(total, rec)
        _cost_add(by_project.setdefault(str(rec["project"]), _cost_bucket()), rec)
    return {**total, "by_project": dict(sorted(by_project.items()))}


def cost_lines(doc: dict[str, Any], *, since: datetime, until: datetime, category: str | None,
               project: str | None) -> list[str]:
    scope = [f"{since.isoformat()} .. {until.isoformat()}"]
    if category:
        scope.append(f"category {category}")
    if project:
        scope.append(f"project {project}")
    t = doc["tokens"]
    out = [f"cost: {', '.join(scope)}: {doc['sessions']} sessions, total {human(t['total'])}",
           f"  input {human(t['input'])}  output {human(t['output'])}  cache read {human(t['cache_read'])}  "
           f"cache write {human(t['cache_creation'])}  active {hms(doc['active_seconds'])}  "
           f"api {hms(doc['api_seconds'])}  tool {hms(doc['tool_seconds'])}"]
    for name, b in doc["by_project"].items():
        out.append(f"  {name[:40]:<40}  {b['sessions']:>4} sessions  total {human(b['tokens']['total']):>7}  "
                   f"active {hms(b['active_seconds'])}")
    return out


def median(values: list[float]) -> float:
    s = sorted(values)
    n = len(s)
    mid = s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2
    return round(mid, 3)


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


def hms(seconds: float | None) -> str:
    """3725 -> 1h02m, 95 -> 1m35s, None -> -."""
    if seconds is None:
        return "-"
    s = int(round(seconds))
    if s >= 3600:
        return f"{s // 3600}h{(s % 3600) // 60:02d}m"
    if s >= 60:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s}s"


def short_id(sid: str) -> str:
    head, _, tail = sid.partition("/")
    prefix, _, rest = head.rpartition(":")
    head = (prefix + ":" if prefix else "") + rest[:8]
    return f"{head}/{tail[:16]}" if tail else head


def _group_line(g: dict[str, Any], width: int, indent: str = "  ") -> str:
    count = f"{g['sessions']} sessions"
    time = f"  active {hms(g['active_seconds'])} (median {hms(g['active_median_seconds'])})" \
        if g.get("active_seconds") is not None else ""
    return (f"{indent}{g['key'][:40]:<{width}}  {count:>13}  total {human(g['total']):>7}  "
            f"out {human(g['output']):>7}{time}")


def lines(doc: dict[str, Any], by: tuple[str, ...] = DEFAULT_BY) -> list[str]:
    """The report as plain text, aligned for a monospace chat block. `by`: the sections;
    `category` lists each category's projects under it."""
    t = doc["totals"]
    scope = [f"last {doc['days']} days"]
    if doc.get("repo"):
        scope.append(f"repo {doc['repo']}")
    if doc.get("tool"):
        scope.append(f"tool {doc['tool']}")
    out = [f"tokens: {', '.join(scope)}: {doc['sessions']} sessions ({doc['measured']} measured), "
           f"total {human(t['total'])}",
           f"  input {human(t['input'])}  output {human(t['output'])}  cache read {human(t['cache_read'])}  "
           f"cache write {human(t['cache_creation'])}  time {hms(doc.get('duration_seconds'))}  "
           f"active {hms(doc.get('active_seconds'))}"]
    if not doc["sessions"]:
        return out
    for dim in by:
        items = doc.get(f"by_{dim}") or []
        if not items:
            continue
        if dim == "category":
            out += ["", "by category / project"]
            names = [g["key"] for g in items] + [p["key"] for g in items for p in g.get("projects", [])]
            width = min(max(len(n) for n in names) + 2, 42)
            for g in items:
                out.append(_group_line(g, width))
                for p in g.get("projects", []):
                    out.append(_group_line(p, width - 2, indent="    "))
            continue
        out += ["", f"by {dim}"]
        width = min(max(len(g["key"]) for g in items), 40)
        for g in items:
            out.append(_group_line(g, width))
    if doc["top"]:
        out += ["", "top sessions"]
        for s in doc["top"]:
            when = (s["last_activity_at"] or "")[:16].replace("T", " ")
            turns = f"{s['turns']} turns" if s.get("turns") is not None else ""
            sub = session_tokens.Tokens.of(s.get("subagent_tokens"))
            subs = f"  (subagents {human(sub.total)})" if sub.total else ""
            place = s.get("project") or s["repo"] or "(none)"
            extra = "  ".join(x for x in (
                s.get("model") or "", s.get("effort") or "", s.get("outcome") or "",
                f"active {hms(s['active_seconds'])}" if s.get("active_seconds") is not None else "",
                f'"{s["title"][:40]}"' if s.get("title") else "") if x)
            out.append(f"  {when:<16}  {s['tool']}/{s['kind']:<8}  {place[:24]:<24}  "
                       f"{short_id(s['session_id']):<26}  total {human(s['total']):>7}  "
                       f"out {human(s['output']):>7}  {turns}{subs}  {extra}".rstrip())
    return out
